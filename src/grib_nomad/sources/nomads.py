"""NOMADS grib_filter source.

NOMADS exposes a CGI 'grib_filter' endpoint per model that takes a directory, a
file, lists of variables/levels, and a sub-region bounding box, and returns a
GRIB2 file containing exactly the requested subset. One HTTP GET per
(model, cycle, fhour). URL conventions vary slightly per model; per-model
templates live in the YAML registry under the `extra` field.

Multiple forecast hours are fetched concurrently via a thread pool — each request
is independent and latency-bound, so threads are a near-pure win.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

from grib_nomad.core.cache import atomic_save, cached_path, is_cached
from grib_nomad.core.combine import DownloadedPart
from grib_nomad.sources.base import (
    DownloadRequest,
    ModelSpec,
    PartCallback,
    Source,
    SourceError,
    StatusCallback,
)

NOMADS_BASE = "https://nomads.ncep.noaa.gov"

_RATE_LIMITED_MSG = (
    "NOMADS rate-limited this IP (Akamai \"Over Rate Limit\"). "
    "Wait 5–15 min for the cooldown to clear, then retry. "
    "See https://luckgrib.com/blog/2021/04/19/throttling.html for "
    "background; the client targets ~100 hits/min."
)


class NomadsRateLimitError(SourceError):
    """Special-cased SourceError raised when Akamai 302's us with the
    rate-limit interstitial. The runner treats this as fatal — there's
    no point continuing once the IP is in the cooldown box, and we
    don't want to flood logs with one error message per remaining
    fhour. Subclasses SourceError so existing `except SourceError`
    handlers still catch it; downstream code that wants to
    differentiate uses `isinstance(...)`.
    """


class _TokenBucket:
    """Thread-safe sliding-rate limiter.

    Maintains a bucket of `capacity` tokens that refills at
    `refill_per_sec`. `acquire()` blocks until ≥1 token is available,
    consumes it, and returns. Used to enforce a steady hits/minute
    ceiling against NOMADS regardless of how many worker threads are
    queued — the concurrent-request semaphore is a separate, looser
    cap to prevent unbounded request fan-out during long idle periods
    when the bucket is full.
    """

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = float(capacity)
        # Start empty, not full: a full bucket at process start lets the
        # first `capacity` requests fire back-to-back, which trips
        # Akamai's burst gate well before the steady-state rate matters.
        self.tokens = 0.0
        self.refill_per_sec = float(refill_per_sec)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self._last) * self.refill_per_sec,
                )
                self._last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait_for = (1.0 - self.tokens) / self.refill_per_sec
            time.sleep(max(0.01, wait_for))


class NomadsSource(Source):
    name = "nomads"

    # NOAA's published guidance per the LuckGrib reverse-engineering note
    # (https://luckgrib.com/blog/2021/04/19/throttling.html) is **120
    # hits/minute**. We default to ~100/min with a small burst margin,
    # but the real Akamai gate is burst-sensitive: a full bucket at
    # process start lets `capacity` requests race out the door in <5s,
    # which trips Akamai well before the steady-state rate ever matters.
    # _TokenBucket starts empty (see __init__), so even with capacity=100
    # the first 60s are paced by the refill rate (~1.67/sec).
    #
    # Override via NomadsSource.configure_rate(rate_per_min=..., concurrency=...)
    # at process startup (e.g. from CLI flags) to dial down for shared
    # egress IPs (CGNAT, office NAT) or to ratchet back up after probing.
    _global_rate_bucket = _TokenBucket(capacity=100.0, refill_per_sec=100.0 / 60.0)
    _global_request_semaphore = threading.Semaphore(4)

    @classmethod
    def configure_rate(
        cls,
        *,
        rate_per_min: float | None = None,
        concurrency: int | None = None,
    ) -> None:
        """Replace the class-level token bucket and/or concurrency gate.

        Call once at process startup, before constructing any NomadsSource.
        The new bucket starts empty (same as the default), so the change
        takes effect from the very first request. In-flight semaphore
        holders are not re-gated — concurrency changes only apply to
        slots acquired after this call.
        """
        if rate_per_min is not None:
            if rate_per_min <= 0:
                raise ValueError("rate_per_min must be > 0")
            cls._global_rate_bucket = _TokenBucket(
                capacity=float(rate_per_min),
                refill_per_sec=float(rate_per_min) / 60.0,
            )
        if concurrency is not None:
            if concurrency < 1:
                raise ValueError("concurrency must be >= 1")
            cls._global_request_semaphore = threading.Semaphore(int(concurrency))

    # Once Akamai's edge layer has 302'd us with an "Over Rate Limit" page,
    # every subsequent request will get the same response until the IP
    # cooldown expires (5–60 min). Continuing to hammer just keeps the
    # cooldown rolling. Trip this flag on first detection and fail every
    # other queued worker fast with a clear actionable message.
    _rate_limited = False
    _rate_limit_lock = threading.Lock()

    def __init__(
        self,
        *,
        base_url: str = NOMADS_BASE,
        session: requests.Session | None = None,
        timeout: float = 60.0,
        retries: int = 3,
        retry_backoff: float = 2.0,
        max_workers: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or _build_session()
        self.session.headers.setdefault(
            "User-Agent", "grib_nomad/0.1 (+https://nomads.ncep.noaa.gov)"
        )
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.max_workers = max(1, max_workers)

    # --- URL building -----------------------------------------------------

    def build_url_and_params(
        self,
        request: DownloadRequest,
        fhour: int,
    ) -> tuple[str, dict]:
        """Return (url, params) for one fhour download via grib_filter."""
        model = request.model
        extra = model.extra
        try:
            filter_path = extra["filter_path"]
            dir_template = extra["dir_template"]
            file_template = extra["file_template"]
        except KeyError as e:
            raise SourceError(
                f"model {model.id} is missing NOMADS template field {e.args[0]!r}"
            ) from e

        cycle_hour = request.cycle_hour
        date_str = request.cycle_date.strftime("%Y%m%d")
        ctx = {"date": date_str, "cycle": cycle_hour, "fhour": fhour}
        directory = dir_template.format(**ctx)
        filename = file_template.format(**ctx)

        cat = model.categories.get(request.category)
        if cat is None:
            raise SourceError(
                f"model {model.id} does not support category {request.category!r}"
            )

        params: dict = {
            "dir": directory,
            "file": filename,
        }
        for v in cat.vars:
            params[f"var_{v}"] = "on"
        for lev in cat.levels:
            params[f"lev_{lev}"] = "on"

        bbox = request.bbox
        params["subregion"] = ""
        params["toplat"] = f"{bbox.lat_max}"
        params["bottomlat"] = f"{bbox.lat_min}"
        params["leftlon"] = f"{bbox.lon_min}"
        params["rightlon"] = f"{bbox.lon_max}"

        url = f"{self.base_url}{filter_path}"
        return url, params

    # --- HTTP -------------------------------------------------------------

    # Note: the retry loop is now inlined into `_fetch_one` so the
    # global request semaphore can be acquired per-attempt (and
    # released during sleep between attempts). The previous
    # `_get_with_retry` helper held the semaphore through backoff
    # sleeps and starved every other worker.

    # --- Download ---------------------------------------------------------

    def download(
        self,
        request: DownloadRequest,
        dest_dir: Path,
        *,
        on_part: PartCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[DownloadedPart]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        cycle_dt = datetime.combine(
            request.cycle_date, datetime.min.time()
        ).replace(hour=request.cycle_hour, tzinfo=timezone.utc)

        workers = min(self.max_workers, len(request.fhours))
        parts: list[DownloadedPart] = []
        errors: list[tuple[int, Exception]] = []
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=f"nomads-{request.model.id}"
        ) as ex:
            futures = {
                ex.submit(self._fetch_one, request, fhour, dest_dir, cycle_dt): fhour
                for fhour in request.fhours
            }
            for f in as_completed(futures):
                fh = futures[f]
                try:
                    part = f.result()
                    parts.append(part)
                    if on_part is not None:
                        on_part(part)
                except NomadsRateLimitError:
                    # Don't log — the first detection already emitted one
                    # warning when it set the class-level flag. Every
                    # other future would raise the identical error, so
                    # cancel them all and propagate to abort the recipe.
                    for fut in futures:
                        fut.cancel()
                    raise
                except Exception as e:
                    # Per-fhour failures are non-fatal: log and keep going.
                    # Common cases: an individual fhour 404s because NOAA
                    # hasn't published it yet, or the upstream cycle's
                    # forecast horizon is shorter than the YAML claims for
                    # this specific cycle hour. Downstream interpolation
                    # bridges small gaps; total failure (zero parts) is
                    # the only thing worth raising for.
                    errors.append((fh, e))
                    log.warning(
                        "%s f%03d fetch failed: %s",
                        request.model.id, fh, e,
                    )

        if errors and not parts:
            # Every fhour failed — surface the first error so the recipe
            # actually exits non-zero rather than silently producing nothing.
            raise errors[0][1]

        if errors:
            log.warning(
                "%s: %d of %d %s fhours failed (run continues with partial data)",
                request.model.id, len(errors), len(futures),
                request.category,
            )

        parts.sort(key=lambda p: p.fhour)
        return parts

    def _cache_filename(self, request: DownloadRequest, fhour: int) -> str:
        """Per-fhour cache filename, scoped by (category, bbox, fhour).

        Cycle is part of the parent directory; model_id is part of the
        path above that. So `wind_woods-hole_f024.grb2` is unambiguous
        within `<cache>/nomads/hrrr_conus_hourly/2026050913/`.
        """
        return (
            f"{request.category}_{request.bbox.slug()}_f{fhour:03d}.grb2"
        )

    def _fetch_one(
        self,
        request: DownloadRequest,
        fhour: int,
        dest_dir: Path,
        cycle_dt: datetime,
    ) -> DownloadedPart:
        cat = request.model.categories[request.category]

        # Cache-hit short-circuit. Persistent disk cache scoped by
        # (model, cycle, category, bbox, fhour) — re-running the same
        # recipe with the same cycle returns cached files without
        # touching NOMADS at all. Bbox is part of the filename so a
        # different region triggers a fresh fetch (no risk of serving
        # the wrong subset). Cycle directories get aged out by
        # `core.cache.prune_stale` once their forecast horizon is past.
        cache_filename = self._cache_filename(request, fhour)
        if is_cached(self.name, request.model.id, cycle_dt, cache_filename, min_size=4):
            target_path = cached_path(
                self.name, request.model.id, cycle_dt, cache_filename
            )
            return DownloadedPart(
                path=target_path,
                model_id=request.model.id,
                category=request.category,
                cycle=cycle_dt,
                fhour=fhour,
                bytes_=target_path.stat().st_size,
                source_url=f"cache://{target_path}",
                bbox=request.bbox.to_dict(),
                variables=list(cat.vars),
                levels=list(cat.levels),
            )

        # If a previous request in this run already tripped the rate
        # limiter, fail fast — banging on the closed door just keeps the
        # cooldown clock rolling, and 660 identical retries flood logs.
        if type(self)._rate_limited:
            raise NomadsRateLimitError(_RATE_LIMITED_MSG)

        target_path = cached_path(
            self.name, request.model.id, cycle_dt, cache_filename
        )
        url, params = self.build_url_and_params(request, fhour)

        last_exc: Exception | None = None
        last_url = url
        for attempt in range(self.retries):
            # Each attempt consumes one token from the global rate bucket
            # before the request goes out — this is the real NOAA rate
            # gate (~100/min). Token-acquisition blocks if we'd exceed
            # the rate; happens *outside* the semaphore so the slot is
            # only held during the actual HTTP exchange.
            type(self)._global_rate_bucket.acquire()
            try:
                with type(self)._global_request_semaphore:
                    resp = self.session.get(
                        url,
                        params=params,
                        timeout=self.timeout,
                        stream=True,
                    )
                    last_url = resp.url
                    if resp.status_code == 200:
                        # Save atomically into the persistent cache. Body is
                        # streamed straight to the temp file so we don't
                        # double-buffer. After save we verify the file
                        # actually starts with the GRIB2 magic — if not,
                        # the response was empty (YAML var/level mismatch
                        # silently returns 200 + 0 bytes) or HTML (rate
                        # limit interstitial that somehow got 200'd) and
                        # we surface that as a clean failure rather than
                        # poison the cache with non-GRIB content.
                        size_holder = [0]

                        def _writer(tmp_path: Path) -> None:
                            with tmp_path.open("wb") as f:
                                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                                    if chunk:
                                        f.write(chunk)
                                        size_holder[0] += len(chunk)

                        atomic_save(target_path, _writer)
                        if not _looks_like_grib2(target_path):
                            try:
                                target_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                            raise SourceError(
                                f"response from {resp.url} is not GRIB2 "
                                f"(got {size_holder[0]} bytes, no GRIB magic) "
                                f"— check the model's category levels"
                            )
                        return DownloadedPart(
                            path=target_path,
                            model_id=request.model.id,
                            category=request.category,
                            cycle=cycle_dt,
                            fhour=fhour,
                            bytes_=size_holder[0],
                            source_url=resp.url,
                            bbox=request.bbox.to_dict(),
                            variables=list(cat.vars),
                            levels=list(cat.levels),
                        )

                    # Non-200: peek at the body to detect Akamai's rate-
                    # limit interstitial. If that's what it is, trip the
                    # class-level flag so siblings don't keep retrying,
                    # close the connection, and bail with a clear message.
                    body_preview = b""
                    try:
                        body_preview = resp.raw.read(2048) if resp.raw else b""
                    except Exception:
                        pass
                    try:
                        resp.close()
                    except Exception:
                        pass

                    if (
                        resp.status_code == 302
                        and (
                            b"Over Rate Limit" in body_preview
                            or b"abusive-user-block" in body_preview
                        )
                    ):
                        with type(self)._rate_limit_lock:
                            if not type(self)._rate_limited:
                                type(self)._rate_limited = True
                                log.warning(
                                    "NOMADS rate-limited this IP at the "
                                    "Akamai edge — failing fast for the "
                                    "rest of this run"
                                )
                        raise NomadsRateLimitError(_RATE_LIMITED_MSG)

                    if resp.status_code in (404, 410):
                        raise SourceError(
                            f"upstream returned {resp.status_code} for "
                            f"{resp.url}; the requested file may not yet "
                            f"be published"
                        )
                    last_exc = SourceError(
                        f"upstream returned {resp.status_code} for {resp.url}"
                    )
            except requests.RequestException as e:
                last_exc = e
            # Sleep happens OUTSIDE the semaphore so the slot is freed.
            time.sleep(self.retry_backoff * (attempt + 1))

        raise SourceError(
            f"failed to GET {last_url} after {self.retries} attempts: "
            f"{last_exc}"
        ) from last_exc


def _looks_like_grib2(path: Path) -> bool:
    """Cheap sanity check that a freshly-written file is actually GRIB2.

    Catches three failure modes that grib_filter can return as 200 OK:
    a 0-byte body (var/level filter matched nothing), an HTML
    interstitial (rate-limit page that bypassed our 302 detection),
    and stray text/error pages. GRIB2 messages always start with the
    ASCII bytes 'GRIB'.
    """
    try:
        with path.open("rb") as f:
            return f.read(4) == b"GRIB"
    except OSError:
        return False


def _build_session() -> requests.Session:
    """Session with a beefier connection pool sized for threaded downloads.

    Pool sized to handle a multi-tier multi-category recipe: a routing run
    with 6+ NOMADS download specs × 6+ inner workers each can put 36+
    concurrent requests in flight against a single host. Default urllib3
    pool of 10 (or our previous 24) was too small and caused threads to
    block waiting for free connections — visible as NOMADS sources stuck
    at 0/N for minutes while the GoMOFS h5netcdf fallback ate bandwidth.
    """
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=64)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _part_filename(request: DownloadRequest, fhour: int) -> str:
    return (
        f"{request.model.id}_{request.category}_"
        f"{request.cycle_date.strftime('%Y%m%d')}_"
        f"t{request.cycle_hour:02d}z_f{fhour:03d}.grb2"
    )


def model_supports_fhour(model: ModelSpec, fhour: int) -> bool:
    if fhour < 0 or fhour > model.max_fhour:
        return False
    for rule in model.steps:
        if rule.from_hour <= fhour <= rule.to_hour and (fhour - rule.from_hour) % rule.step == 0:
            return True
    return False
