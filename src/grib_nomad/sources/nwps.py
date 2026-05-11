"""NWPS (Nearshore Wave Prediction System) source.

NWPS is SWAN driven by GFS Wave open-boundary conditions, run by each NWS
coastal Weather Forecast Office at ~1–3 km resolution — much finer than GFS
Wave's 0.16° (~17 km). It actually resolves Vineyard/Nantucket Sound, Long
Island Sound, Chesapeake Bay, and the rest of the nearshore where GFS Wave
masks everything as land.

NOMADS layout differs from our other models. Instead of one GRIB2 per
forecast-hour, NWPS publishes ONE GRIB2 per (WFO, cycle, computational
grid) that contains every forecast hour bundled together. Example:

    /pub/data/nccf/com/nwps/prod/er.YYYYMMDD/box/HH/CG1/
        box_nwps_CG1_YYYYMMDD_HH00.grib2     (~44 MB)

So this source:

  1. Fetches the per-cycle bundle ONCE via `/cgi-bin/filter_ernwps.pl` with
     variable / level / bbox subsetting. After filtering the bundle is
     typically 1–5 MB per category for a small recipe bbox.
  2. Splits the bundle locally into per-fhour cache files, keyed the same
     way as NOMADS per-fhour caches so the combine / manifest layers don't
     have to special-case anything.
  3. Returns a `DownloadedPart` list pointing at the per-fhour files, sorted.

Rate limiting: NWPS is the same host as the rest of NOMADS, so we reuse
`NomadsSource`'s class-level token bucket + semaphore + rate-limit flag.
Treating NWPS as a separate "budget" would let one source starve the other
of NOAA's ~120-hits/min ceiling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from grib_nomad.core.cache import atomic_save, cached_path, is_cached
from grib_nomad.core.combine import DownloadedPart
from grib_nomad.sources.base import (
    DownloadRequest,
    PartCallback,
    Source,
    SourceError,
    StatusCallback,
)
from grib_nomad.sources.nomads import (
    NOMADS_BASE,
    NomadsRateLimitError,
    NomadsSource,
    _build_session,
    _looks_like_grib2,
    _RATE_LIMITED_MSG,
)

log = logging.getLogger(__name__)


class NwpsSource(Source):
    name = "nwps"

    def __init__(
        self,
        *,
        base_url: str = NOMADS_BASE,
        session: requests.Session | None = None,
        timeout: float = 120.0,
        retries: int = 3,
        retry_backoff: float = 2.0,
    ):
        # NWPS bundle downloads are larger than per-fhour NOMADS files (a few
        # MB vs. a few KB), so a longer default timeout. Retries / backoff
        # match NomadsSource defaults.
        self.base_url = base_url.rstrip("/")
        self.session = session or _build_session()
        self.session.headers.setdefault(
            "User-Agent", "grib_nomad/0.1 (+https://nomads.ncep.noaa.gov)"
        )
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff

    # --- URL building -----------------------------------------------------

    def _build_url_and_params(self, request: DownloadRequest) -> tuple[str, dict]:
        """One bundle URL per (cycle, category, bbox). No fhour in template."""
        model = request.model
        extra = model.extra
        try:
            filter_path = extra["filter_path"]
            dir_template = extra["dir_template"]
            file_template = extra["file_template"]
            wfo = extra["wfo"]
        except KeyError as e:
            raise SourceError(
                f"model {model.id} is missing NWPS template field {e.args[0]!r}"
            ) from e

        date_str = request.cycle_date.strftime("%Y%m%d")
        ctx = {
            "date": date_str,
            "cycle": request.cycle_hour,
            "wfo": wfo,
        }
        directory = dir_template.format(**ctx)
        filename = file_template.format(**ctx)

        cat = model.categories.get(request.category)
        if cat is None:
            raise SourceError(
                f"model {model.id} does not support category {request.category!r}"
            )

        params: dict = {"dir": directory, "file": filename}
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

    # --- Cache filenames --------------------------------------------------

    def _bundle_filename(self, request: DownloadRequest) -> str:
        # Per category + bbox; cycle is in the parent directory.
        return f"{request.category}_{request.bbox.slug()}_bundle.grb2"

    def _fhour_filename(self, request: DownloadRequest, fhour: int) -> str:
        return f"{request.category}_{request.bbox.slug()}_f{fhour:03d}.grb2"

    # --- Public download API ---------------------------------------------

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

        def _status(msg: str) -> None:
            if on_status is not None:
                on_status(msg)

        # 1. Fast path: if every requested fhour already has a per-fhour
        #    cache file, we don't need the bundle at all.
        cat = request.model.categories[request.category]
        all_cached = all(
            is_cached(
                self.name,
                request.model.id,
                cycle_dt,
                self._fhour_filename(request, fh),
                min_size=4,
            )
            for fh in request.fhours
        )
        if not all_cached:
            # 2. Make sure the bundle is on disk (download once if missing).
            bundle_path = self._fetch_bundle_if_needed(
                request, cycle_dt, status=_status
            )
            # 3. Split bundle into per-fhour cache files. Cheap (it's already local).
            self._split_bundle_to_fhour_caches(
                request, cycle_dt, bundle_path, status=_status
            )

        # 4. Build the parts list from the per-fhour cache. Some fhours
        #    may not exist in the bundle (NOAA shortens the run on some
        #    cycles); skip those rather than fail the whole category.
        parts: list[DownloadedPart] = []
        missing: list[int] = []
        for fh in request.fhours:
            fname = self._fhour_filename(request, fh)
            if not is_cached(self.name, request.model.id, cycle_dt, fname, min_size=4):
                missing.append(fh)
                continue
            path = cached_path(self.name, request.model.id, cycle_dt, fname)
            part = DownloadedPart(
                path=path,
                model_id=request.model.id,
                category=request.category,
                cycle=cycle_dt,
                fhour=fh,
                bytes_=path.stat().st_size,
                source_url=f"cache://{path}",
                bbox=request.bbox.to_dict(),
                variables=list(cat.vars),
                levels=list(cat.levels),
            )
            parts.append(part)
            if on_part is not None:
                on_part(part)

        if missing:
            log.warning(
                "%s: %d of %d %s fhours not present in the bundle (run "
                "continues with partial data); missing examples: %s",
                request.model.id,
                len(missing),
                len(request.fhours),
                request.category,
                missing[:5],
            )

        if not parts:
            raise SourceError(
                f"NWPS bundle for {request.model.id} cycle "
                f"{cycle_dt.isoformat()} did not yield any of the requested "
                f"fhours; bundle may be empty or category vars don't exist"
            )

        parts.sort(key=lambda p: p.fhour)
        return parts

    # --- Bundle fetch (with NOMADS rate-limiting) ------------------------

    def _fetch_bundle_if_needed(
        self,
        request: DownloadRequest,
        cycle_dt: datetime,
        *,
        status,
    ) -> Path:
        bundle_filename = self._bundle_filename(request)
        if is_cached(
            self.name, request.model.id, cycle_dt, bundle_filename, min_size=64
        ):
            return cached_path(
                self.name, request.model.id, cycle_dt, bundle_filename
            )

        # Share NomadsSource's edge-rate limiter. NWPS sits behind the same
        # Akamai layer; treating it as a separate budget would let the two
        # sources collectively exceed the ~120/min cap.
        if NomadsSource._rate_limited:
            raise NomadsRateLimitError(_RATE_LIMITED_MSG)

        url, params = self._build_url_and_params(request)
        target_path = cached_path(
            self.name, request.model.id, cycle_dt, bundle_filename
        )
        status(f"fetching NWPS bundle ({request.category}, {request.bbox.slug()})")

        last_exc: Exception | None = None
        last_url = url
        for attempt in range(self.retries):
            NomadsSource._global_rate_bucket.acquire()
            try:
                with NomadsSource._global_request_semaphore:
                    resp = self.session.get(
                        url, params=params, timeout=self.timeout, stream=True
                    )
                    last_url = resp.url
                    if resp.status_code == 200:
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
                                f"NWPS response from {resp.url} is not GRIB2 "
                                f"(got {size_holder[0]} bytes, no GRIB magic) "
                                f"— check var/level names in nwps_models.yaml"
                            )
                        return target_path

                    # Non-200: check for Akamai rate-limit interstitial
                    body_preview = b""
                    try:
                        body_preview = resp.raw.read(2048) if resp.raw else b""
                    except Exception:
                        pass
                    try:
                        resp.close()
                    except Exception:
                        pass

                    if resp.status_code == 302 and (
                        b"Over Rate Limit" in body_preview
                        or b"abusive-user-block" in body_preview
                    ):
                        with NomadsSource._rate_limit_lock:
                            if not NomadsSource._rate_limited:
                                NomadsSource._rate_limited = True
                                log.warning(
                                    "NOMADS rate-limited this IP at the "
                                    "Akamai edge (via NWPS) — failing fast "
                                    "for the rest of this run"
                                )
                        raise NomadsRateLimitError(_RATE_LIMITED_MSG)

                    if resp.status_code in (404, 410):
                        raise SourceError(
                            f"NWPS upstream returned {resp.status_code} for "
                            f"{resp.url}; the requested cycle may not yet "
                            f"be published"
                        )
                    last_exc = SourceError(
                        f"NWPS upstream returned {resp.status_code} for {resp.url}"
                    )
            except requests.RequestException as e:
                last_exc = e
            import time as _time
            _time.sleep(self.retry_backoff * (attempt + 1))

        raise SourceError(
            f"failed to GET NWPS bundle {last_url} after {self.retries} "
            f"attempts: {last_exc}"
        ) from last_exc

    # --- Bundle → per-fhour split (eccodes) ------------------------------

    def _split_bundle_to_fhour_caches(
        self,
        request: DownloadRequest,
        cycle_dt: datetime,
        bundle_path: Path,
        *,
        status,
    ) -> None:
        """Walk the bundle once, group raw GRIB2 messages by `forecastTime`,
        and write one per-fhour cache file per group.

        Uses eccodes' `codes_get_message` to copy each message's bytes
        verbatim — we never re-encode, just slice. So no risk of altering
        scaling, packing, bitmaps, etc.; the per-fhour files are byte-
        identical concatenations of the bundle's original messages.
        """
        try:
            from eccodes import (
                codes_get,
                codes_get_message,
                codes_grib_new_from_file,
                codes_release,
            )
        except ImportError as e:
            raise SourceError(
                "NWPS source requires `eccodes` to split the per-cycle "
                "bundle into per-fhour files. Install the [gomofs] extra: "
                "`conda install -c conda-forge eccodes`."
            ) from e

        status(f"splitting NWPS bundle by forecast hour ({bundle_path.name})")
        per_fhour: dict[int, list[bytes]] = {}
        with bundle_path.open("rb") as fin:
            while True:
                gid = codes_grib_new_from_file(fin)
                if gid is None:
                    break
                try:
                    fhour = int(codes_get(gid, "forecastTime"))
                    per_fhour.setdefault(fhour, []).append(codes_get_message(gid))
                finally:
                    codes_release(gid)

        if not per_fhour:
            raise SourceError(
                f"NWPS bundle {bundle_path} contained no GRIB2 messages — "
                f"check filter_path / var / level configuration"
            )

        for fh, blobs in per_fhour.items():
            target = cached_path(
                self.name,
                request.model.id,
                cycle_dt,
                self._fhour_filename(request, fh),
            )
            if target.exists():
                continue  # already split from a previous (interrupted) run

            payload = b"".join(blobs)

            def _writer(tmp_path: Path, data: bytes = payload) -> None:
                tmp_path.write_bytes(data)

            atomic_save(target, _writer)
