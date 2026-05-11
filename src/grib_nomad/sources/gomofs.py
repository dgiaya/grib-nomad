"""GoMOFS source — Gulf of Maine OFS surface currents (with tides) from
NOAA Open Data on AWS S3, fetched as HDF5 chunked reads via h5netcdf+s3fs
so we only pull the variables we actually use.

Background
----------

GoMOFS is a ROMS model on a 777×1173 curvilinear C-grid. The hourly `2ds`
product on `s3://noaa-ofs-pds/gomofs.YYYYMMDD/...` packs many surface fields
into one ~170 MB NetCDF: u_sur/v_sur for currents, plus zeta, salt, temp,
atmospheric forcing (Pair/U/V/T), 8 wet/dry masks, ROMS scalars, and the
curvilinear coordinates and metrics. We only need ~14 MB of that:

  - per-cycle static: lon_u/lat_u, lon_v/lat_v, lon_rho/lat_rho, mask_rho
    (~37 MB, downloaded once per cycle, then cached on disk)
  - per-fhour: u_sur, v_sur (~7 MB per file)

By opening with `engine="h5netcdf"` + `storage_options={"anon": True}` xarray
and h5netcdf only fetch the HDF5 chunks for variables we read. Bandwidth drops
from 170 MB → ~7 MB per fhour after the one-time static fetch. The on-disk
cache stores the small extracted NetCDFs, not the full originals.

Pipeline per recipe-cycle:

  1. Build target regular lat/lon grid covering the recipe bbox.
  2. Fetch (or cache-hit) the cycle-static file: coords + rho mask.
  3. Build cKDTree-based nearest-node index maps from each staggered
     ROMS grid (lon_u/lat_u and lon_v/lat_v) to the target grid. One-time;
     reused for every fhour of this cycle.
  4. Per fhour, in parallel: fetch (or cache-hit) the u/v subset file,
     index via the precomputed maps, write a 2-message GRIB2 (UOGRD + VOGRD).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grib_nomad.core.cache import (
    atomic_save,
    cached_path,
    is_cached,
    is_model_cached,
    model_cached_path,
)
from grib_nomad.core.combine import DownloadedPart
from grib_nomad.sources.base import (
    DownloadRequest,
    PartCallback,
    Source,
    SourceError,
    StatusCallback,
)

S3_BASE_URL = "https://noaa-ofs-pds.s3.amazonaws.com"
S3_BUCKET_URL = "s3://noaa-ofs-pds"

log = logging.getLogger(__name__)

# GRIB2 parameter codes (WMO Code Table 4.2-10-1: oceanographic > currents)
_GRIB_DISCIPLINE_OCEAN = 10
_GRIB_CATEGORY_CURRENTS = 1
_GRIB_PARAM_U_CURRENT = 2  # UOGRD
_GRIB_PARAM_V_CURRENT = 3  # VOGRD
_MISSING_VALUE = 9.999e20

# Variables we extract from the 2ds file and cache locally.
#
# Important: we need `angle` (rho-grid rotation between model xi-axis and
# geographic east) to convert u_sur/v_sur from ROMS curvilinear (xi/eta)
# components to east/north — without this rotation the GRIB output's vectors
# point in the wrong direction along the coast, where the GoMOFS grid follows
# the bathymetry and isn't aligned with lat/lon.
_STATIC_VARS = ["lon_rho", "lat_rho", "mask_rho", "angle"]
_FHOUR_VARS = ["u_sur", "v_sur"]


@dataclass
class _CycleContext:
    """Static state shared across every forecast-hour fetch in one cycle.

    All target → rho lookups go through `rho_water_indices`, which only ever
    points at *water* rho cells — built that way deliberately so we never mix
    land sentinel values into the output and so narrow harbors/channels that
    sub-resolve the rho grid still get plausible currents.
    """

    cycle_dt: datetime
    target_lat: Any
    target_lon: Any
    rho_water_indices: Any   # shape (n_target,), flat indices into rho-grid water cells
    rho_water_distances: Any  # shape (n_target,), degrees to nearest water cell
    in_domain: Any           # bool, shape (n_target,)
    over_water_mask: Any     # bool, shape (nlat_target, nlon_target) — = in_domain reshaped
    angle: Any               # shape (eta_rho, xi_rho), radians (xi-axis vs. east)
    rho_shape: tuple[int, int]  # (eta_rho, xi_rho)


class _TransientFetchError(SourceError):
    """Raised when a chunk Range fetch fails on transient causes (connection
    closed mid-flight, 5xx, timeout) even after bounded retries.

    Distinct from systemic SourceErrors (missing chunk refs, compressor/filters
    on the data, etc.) so that the global `_kerchunk_disabled` flag only trips
    on the latter — a single bad-network fhour shouldn't condemn the remaining
    fhours to the much slower h5netcdf path.
    """


def _missing_dep(name: str) -> SourceError:
    return SourceError(
        f"GoMOFS source requires `{name}` — install the [gomofs] extra "
        f"(via conda-forge: `conda install -c conda-forge xarray netCDF4 "
        f"h5netcdf s3fs scipy eccodes`)."
    )


class GomofsSource(Source):
    name = "gomofs"

    def __init__(
        self,
        *,
        s3_https_url: str = S3_BASE_URL,
        s3_bucket_url: str = S3_BUCKET_URL,
        target_lat_step: float = 0.01,
        target_lon_step: float | None = None,
        max_dist_to_gomofs_water_deg: float = 0.5,
        coastline_samples_per_cell: int = 3,
        max_workers: int = 16,
        kerchunk_min_fhours: int = 24,
    ):
        """Output masking is a two-step AND:

        1. Coastline check (the real one): for each output cell, sample
           `coastline_samples_per_cell^2` interior points against
           `global-land-mask` (1 km Natural-Earth-derived land raster).
           Cell counts as water if *any* sample is over water — i.e. the
           cell rectangle isn't entirely on land.
        2. GoMOFS-domain sanity check: the cell's nearest GoMOFS water
           rho cell is within `max_dist_to_gomofs_water_deg` (default
           0.5° ≈ 55 km — generous, only excludes targets that fall well
           outside the GoMOFS domain).

        The coastline check is what stops "currents painted onto Cape Cod's
        interior"; the GoMOFS-domain check just ensures we don't extrapolate
        wildly when the recipe bbox extends beyond the model.
        """
        self.s3_https_url = s3_https_url.rstrip("/")
        self.s3_bucket_url = s3_bucket_url.rstrip("/")
        self.target_lat_step = target_lat_step
        self.target_lon_step = target_lon_step or target_lat_step
        self.max_dist_to_gomofs_water_deg = max_dist_to_gomofs_water_deg
        self.coastline_samples_per_cell = max(1, coastline_samples_per_cell)
        self.max_workers = max(1, max_workers)
        # Kerchunk metadata walk is ~minutes on slow links because HDF5's
        # walk pattern is many small scattered reads. The walk only pays off
        # when amortized across many fhours — for short requests we skip it
        # and go straight to per-fhour h5netcdf opens (also slow per file
        # but parallelizes properly). Already-cached refs are always used
        # regardless of request size.
        self.kerchunk_min_fhours = max(1, kerchunk_min_fhours)

    # --- URL building -----------------------------------------------------

    def _file_paths(
        self, model_extra: dict, cycle_dt: datetime, fhour: int
    ) -> tuple[str, str, str]:
        """Return (s3_uri, https_url, filename) for one fhour's 2ds file."""
        ofs_id = model_extra.get("ofs_id", "gomofs")
        template = model_extra["file_template"]
        ctx = {
            "ofs_id": ofs_id,
            "date": cycle_dt.strftime("%Y%m%d"),
            "cycle": cycle_dt.hour,
            "fhour": fhour,
        }
        rel_path = template.format(**ctx)
        s3_uri = f"{self.s3_bucket_url}/{rel_path}"
        https_url = f"{self.s3_https_url}/{rel_path}"
        filename = rel_path.split("/")[-1]
        return s3_uri, https_url, filename

    # --- Public API -------------------------------------------------------

    def download(
        self,
        request: DownloadRequest,
        dest_dir: Path,
        *,
        on_part: PartCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[DownloadedPart]:
        try:
            import numpy as np  # noqa: F401
            import xarray as xr  # noqa: F401
            from scipy.spatial import cKDTree  # noqa: F401
        except ImportError as e:
            raise _missing_dep(e.name or "xarray/numpy/scipy") from e

        self._check_bbox_intersects_domain(request)
        dest_dir.mkdir(parents=True, exist_ok=True)

        cycle_dt = datetime.combine(
            request.cycle_date, datetime.min.time()
        ).replace(hour=request.cycle_hour)
        if cycle_dt.tzinfo is None:
            cycle_dt = cycle_dt.replace(tzinfo=timezone.utc)

        def _status(msg: str) -> None:
            if on_status is not None:
                on_status(msg)

        # 1. Decide whether to use the kerchunk path. If refs are already
        #    cached for this cycle we always use them (free win); otherwise
        #    we only build them when the request has enough fhours to
        #    amortize the (expensive on slow links!) HDF5 metadata walk.
        n_fhours = len(request.fhours)
        refs_cached = is_model_cached(
            self.name,
            request.model.id,
            self._refs_filename(),
            min_size=100,
        )
        use_kerchunk = refs_cached or n_fhours >= self.kerchunk_min_fhours

        cycle_refs: dict | None = None
        if use_kerchunk:
            if refs_cached:
                _status("loading cached kerchunk refs")
            else:
                _status(
                    f"walking HDF5 metadata (kerchunk; ~minutes on slow links, "
                    f"one-time for this cycle)"
                )
            cycle_refs = self._build_or_load_cycle_refs(
                request.model, cycle_dt, request.fhours[0]
            )
        else:
            log.info(
                "GoMOFS: %d fhours < kerchunk threshold %d and no cached refs; "
                "using per-fhour h5netcdf path",
                n_fhours,
                self.kerchunk_min_fhours,
            )

        # 2. Static + first-fhour subset (combined into one open of f001).
        _status("fetching static coords + first fhour")
        static_path = self._fetch_cycle_static(
            request.model, cycle_dt, request.fhours[0], cycle_refs
        )

        # 3. Build target grid + nearest-node lookups from the static file
        _status("building cKDTree + GSHHG coastline mask")
        ctx = self._build_cycle_context(
            request.model, cycle_dt, request.bbox, static_path
        )
        _status(
            f"fetching {n_fhours} fhours "
            f"({'kerchunk' if cycle_refs is not None else 'h5netcdf'}, "
            f"{min(self.max_workers, n_fhours)} parallel)"
        )

        # 4. Parallel per-fhour: fetch u/v subset (cache or S3 via kerchunk),
        #    regrid, write GRIB2.
        workers = min(self.max_workers, len(request.fhours))
        parts: list[DownloadedPart] = []
        errors: list[tuple[int, Exception]] = []
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="gomofs"
        ) as ex:
            futures = {
                ex.submit(
                    self._process_one_fhour,
                    request,
                    fhour,
                    dest_dir,
                    cycle_dt,
                    ctx,
                    cycle_refs,
                ): fhour
                for fhour in request.fhours
            }
            for f in as_completed(futures):
                fh = futures[f]
                try:
                    part = f.result()
                    parts.append(part)
                    if on_part is not None:
                        on_part(part)
                except Exception as e:
                    errors.append((fh, e))
                    log.warning(
                        "GoMOFS f%03d fetch failed: %s", fh, e
                    )

        if errors and not parts:
            raise errors[0][1]
        if errors:
            log.warning(
                "GoMOFS: %d of %d fhours failed (run continues with partial data)",
                len(errors), len(futures),
            )

        parts.sort(key=lambda p: p.fhour)
        return parts

    # --- Cache fetch helpers ---------------------------------------------

    # --- Cache filename helpers ------------------------------------------

    def _static_filename(self, cycle_dt: datetime) -> str:
        # Bumping the suffix any time the static schema changes invalidates
        # caches from previous code versions automatically.
        return (
            f"gomofs.t{cycle_dt.hour:02d}z."
            f"{cycle_dt.strftime('%Y%m%d')}.2ds.cycle_static_v2.nc"
        )

    def _fhour_subset_filename(self, cycle_dt: datetime, fhour: int) -> str:
        return (
            f"gomofs.t{cycle_dt.hour:02d}z."
            f"{cycle_dt.strftime('%Y%m%d')}.2ds.f{fhour:03d}.subset.nc"
        )

    def _refs_filename(self) -> str:
        """Cycle-independent kerchunk refs filename — ROMS+netcdf-c writes
        2ds files with a fixed byte layout, so one walk's refs apply to
        every cycle this model has ever produced or will produce. Stored
        at the model level (not per-cycle); only invalidated by a NOAA-side
        schema change or the user explicitly clearing the file.
        """
        return "kerchunk_refs_v1.json"

    # --- Kerchunk: walk metadata once per cycle --------------------------

    def _build_or_load_cycle_refs(
        self,
        model,
        cycle_dt: datetime,
        sample_fhour: int,
    ) -> dict | None:
        """Build (or load from cache) a kerchunk reference dict for this model.

        kerchunk's `SingleHdf5ToZarr` walks the HDF5 metadata of one S3 file
        once and emits a JSON dict that maps every chunk to its (url, byte
        offset, byte length). Crucially, ROMS+netcdf-c writes 2ds files with
        a *deterministic* byte layout — every 2ds file this model has ever
        published, or will publish, shares the same chunk offsets/sizes;
        only the data values change. So one walk produces refs that are
        valid for any cycle, indefinitely. Cached at the model level
        (cycle-independent) and reused until either NOAA changes the schema
        (rare; refs_v1 → v2 bump on our side) or the user manually clears
        the file. On any kerchunk failure we fall back to per-fhour
        h5netcdf opens — the worst case is degraded performance, never bad
        data, because the kerchunk fetch path wraps in a try/except.
        """
        refs_filename = self._refs_filename()
        refs_path = model_cached_path(self.name, model.id, refs_filename)

        if is_model_cached(
            self.name, model.id, refs_filename, min_size=100
        ):
            try:
                with refs_path.open("r") as f:
                    payload = json.load(f)
                # Backwards-compat: old cycle-level refs were a bare refs
                # dict; new payload wraps refs in a metadata envelope.
                if isinstance(payload, dict) and "refs" in payload and "version" not in payload:
                    refs = payload  # old format; "refs" is part of v1 layout
                elif isinstance(payload, dict) and "_grib_nomad_meta" in payload:
                    refs = payload["refs"]
                else:
                    refs = payload
                log.info(
                    "kerchunk refs cache hit (model-level): %s", refs_path
                )
                return refs
            except Exception as e:
                log.warning(
                    "kerchunk refs cache load failed (%s); rebuilding", e
                )

        try:
            import fsspec
            from kerchunk.hdf import SingleHdf5ToZarr
        except ImportError as e:
            log.warning(
                "kerchunk/fsspec not available (%s); per-fhour fetches will "
                "use h5netcdf path",
                e,
            )
            return None

        s3_uri, _, _ = self._file_paths(model.extra, cycle_dt, sample_fhour)
        log.info(
            "Walking %s HDF5 metadata once-and-forever via %s",
            model.id,
            s3_uri,
        )
        try:
            with fsspec.open(s3_uri, mode="rb", anon=True) as fp:
                chunks = SingleHdf5ToZarr(fp, s3_uri, inline_threshold=300)
                refs = chunks.translate()
        except Exception as e:
            log.warning(
                "kerchunk metadata walk failed for %s (%s); falling back to "
                "h5netcdf",
                s3_uri,
                e,
            )
            return None

        try:
            payload = {
                "_grib_nomad_meta": {
                    "schema_version": 1,
                    "model_id": model.id,
                    "built_from_url": s3_uri,
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "note": (
                        "Cycle-independent: ROMS+netcdf-c byte layout is "
                        "deterministic across all forecast cycles. Reused "
                        "indefinitely; delete this file or bump the schema "
                        "version to force a re-walk."
                    ),
                },
                "refs": refs,
            }
            atomic_save(refs_path, lambda tmp: _write_json(payload, tmp))
            log.info(
                "Cached %s kerchunk refs (model-level, reusable forever): %s",
                model.id,
                refs_path,
            )
        except Exception as e:
            log.warning("failed to cache kerchunk refs (%s)", e)

        return refs

    def _fetch_cycle_static(
        self,
        model,
        cycle_dt: datetime,
        sample_fhour: int,
        cycle_refs: dict | None,
    ) -> Path:
        """Pull static coords + mask once per cycle, AND opportunistically
        the per-fhour u_sur/v_sur subset for `sample_fhour` from the same S3
        open. The opportunistic write avoids a second HDF5 metadata walk on
        the same file, which is the single most expensive part of a per-fhour
        fetch (~10–15 s in our measurements). When `sample_fhour`'s subset is
        already cached, we just pull the static vars.
        """
        static_filename = self._static_filename(cycle_dt)
        fhour_filename = self._fhour_subset_filename(cycle_dt, sample_fhour)
        static_target = cached_path(self.name, model.id, cycle_dt, static_filename)
        fhour_target = cached_path(self.name, model.id, cycle_dt, fhour_filename)

        static_cached = is_cached(
            self.name, model.id, cycle_dt, static_filename, min_size=1024
        )
        fhour_cached = is_cached(
            self.name, model.id, cycle_dt, fhour_filename, min_size=1024
        )

        if static_cached and fhour_cached:
            log.debug("GoMOFS static + first-fhour cache hit: %s", static_target)
            return static_target

        s3_uri, _, _ = self._file_paths(model.extra, cycle_dt, sample_fhour)

        vars_to_pull: list[str] = []
        if not static_cached:
            vars_to_pull.extend(_STATIC_VARS)
        if not fhour_cached:
            vars_to_pull.extend(_FHOUR_VARS)

        log.info(
            "GoMOFS combined static+f%03d fetch: %s -> %s",
            sample_fhour,
            s3_uri,
            ", ".join([static_target.name] if not static_cached else [])
            + (", " if (not static_cached and not fhour_cached) else "")
            + ", ".join([fhour_target.name] if not fhour_cached else []),
        )
        loaded = self._open_data(cycle_refs, s3_uri, vars_to_pull)
        try:
            if not static_cached:
                static_subset = loaded[
                    [v for v in _STATIC_VARS if v in loaded.data_vars]
                ]
                atomic_save(
                    static_target,
                    lambda tmp, s=static_subset: _save_subset_nc(s, tmp),
                )
                static_subset.close()
            if not fhour_cached:
                fhour_subset = loaded[
                    [v for v in _FHOUR_VARS if v in loaded.data_vars]
                ]
                atomic_save(
                    fhour_target,
                    lambda tmp, s=fhour_subset: _save_subset_nc(s, tmp),
                )
                fhour_subset.close()
        finally:
            loaded.close()
        return static_target

    def _fetch_fhour_subset(
        self,
        model,
        cycle_dt: datetime,
        fhour: int,
        cycle_refs: dict | None,
    ) -> Path:
        """Pull u_sur + v_sur for one fhour into the disk cache."""
        filename = self._fhour_subset_filename(cycle_dt, fhour)
        target = cached_path(self.name, model.id, cycle_dt, filename)
        if is_cached(self.name, model.id, cycle_dt, filename, min_size=1024):
            log.debug("GoMOFS fhour cache hit: %s", target)
            return target

        s3_uri, _, _ = self._file_paths(model.extra, cycle_dt, fhour)
        loaded = self._open_data(cycle_refs, s3_uri, _FHOUR_VARS)
        try:
            atomic_save(
                target, lambda tmp, s=loaded: _save_subset_nc(s, tmp)
            )
        finally:
            loaded.close()
        return target

    # --- Data fetch: kerchunk path (preferred) + h5netcdf fallback ------

    # Thread-safe flag: once kerchunk fails for the first time in a process
    # (typically a fsspec/zarr version mismatch), we record it here so the
    # parallel block skips kerchunk for every remaining fhour instead of
    # producing 71 identical warning lines.
    _kerchunk_disabled = False
    _kerchunk_disable_lock = threading.Lock()

    def _open_data(
        self,
        cycle_refs: dict | None,
        s3_uri: str,
        vars_to_pull: list[str],
    ):
        """Load `vars_to_pull` from `s3_uri` — kerchunk path if refs are
        available, h5netcdf fallback otherwise.

        kerchunk path: rewrite the cycle's reference dict to point at
        `s3_uri`, mount it as a zarr-via-fsspec virtual store, fetch only
        the needed chunks via plain HTTP Range. No HDF5 metadata walk.

        After the first per-process kerchunk failure we set a class flag so
        subsequent fetches skip the kerchunk attempt entirely — keeps the
        warning to one line and avoids paying the failure latency 70+ times.
        """
        if cycle_refs is not None and not type(self)._kerchunk_disabled:
            try:
                return self._open_via_kerchunk(cycle_refs, s3_uri, vars_to_pull)
            except _TransientFetchError as e:
                # Network blip on this single file — the chunk loop already
                # retried with backoff. Fall through to h5netcdf for *this*
                # file only; do NOT disable kerchunk globally, because the
                # next fhour gets fresh sockets and likely succeeds.
                log.warning(
                    "GoMOFS kerchunk chunk fetch failed transiently for %s "
                    "(%s); falling through to h5netcdf for this file only",
                    s3_uri,
                    e,
                )
            except Exception as e:
                with type(self)._kerchunk_disable_lock:
                    if not type(self)._kerchunk_disabled:
                        type(self)._kerchunk_disabled = True
                        log.warning(
                            "kerchunk path failed systemically (%s) — "
                            "disabling for rest of this run, falling back "
                            "to h5netcdf for all remaining fhours",
                            e,
                        )
        return self._open_and_load(s3_uri, vars_to_pull)

    def _open_via_kerchunk(
        self,
        cycle_refs: dict,
        target_url: str,
        vars_to_pull: list[str],
    ):
        """Fetch `vars_to_pull` via plain HTTP Range requests against the
        kerchunk-recorded byte offsets. Bypasses fsspec.reference + zarr
        entirely.

        Why direct: every layer above us (xarray's zarr backend, fsspec's
        reference filesystem, zarr v2/v3 metadata negotiation, async/sync
        handshakes) has had latent compatibility issues across versions. Our
        GoMOFS 2ds files have a trivial layout — each variable is *one*
        uncompressed chunk, so the chunk reference `[url, offset, size]`
        from kerchunk is literally pointing at a contiguous block of raw
        little-endian float bytes in the S3 file. Read those bytes,
        `np.frombuffer` + reshape, done. No zarr.

        Refuses to handle compressed chunks or chunks with filters — those
        would need decompression / filter inversion. For 2ds that's never
        an issue (we verified `compressor=null, filters=null` for every
        variable). If the layout ever changes, we'll get a clean error
        rather than silent corruption.
        """
        import json as _json
        from concurrent.futures import ThreadPoolExecutor as _Pool
        from concurrent.futures import as_completed as _as_completed
        from itertools import product

        import numpy as np
        import requests
        import xarray as xr

        if not vars_to_pull:
            raise ValueError("vars_to_pull must be non-empty")

        # Walk our nested envelope to the actual chunk-refs dict.
        inner = cycle_refs
        while isinstance(inner, dict) and "refs" in inner and isinstance(
            inner["refs"], dict
        ):
            inner = inner["refs"]

        # kerchunk records `s3://` URLs, but plain HTTP Range works against
        # the bucket's HTTPS endpoint and avoids needing s3fs entirely.
        if target_url.startswith("s3://"):
            rest = target_url[5:]
            slash = rest.index("/")
            bucket, path = rest[:slash], rest[slash + 1 :]
            target_https = f"https://{bucket}.s3.amazonaws.com/{path}"
        else:
            target_https = target_url

        def _parse(v):
            return _json.loads(v) if isinstance(v, str) else v

        def _fetch_var(var_name: str) -> tuple[str, Any, list[str], dict]:
            zarray_key = f"{var_name}/.zarray"
            if zarray_key not in inner:
                raise SourceError(
                    f"kerchunk refs missing `{zarray_key}` for {target_url}"
                )
            zarray = _parse(inner[zarray_key])
            shape = list(zarray["shape"])
            chunks_shape = list(zarray["chunks"])
            dtype = np.dtype(zarray["dtype"])
            if zarray.get("compressor") is not None or zarray.get("filters"):
                raise SourceError(
                    f"{var_name} has compressor/filters in zarray metadata; "
                    f"the direct kerchunk path only supports raw chunks"
                )

            n_per_dim = [
                (shape[d] + chunks_shape[d] - 1) // chunks_shape[d]
                for d in range(len(shape))
            ]
            full = np.empty(tuple(shape), dtype=dtype)

            with requests.Session() as sess:
                for chunk_idx in product(*[range(n) for n in n_per_dim]):
                    chunk_key = f"{var_name}/" + ".".join(
                        str(i) for i in chunk_idx
                    )
                    if chunk_key not in inner:
                        raise SourceError(
                            f"kerchunk refs missing chunk `{chunk_key}` "
                            f"for {target_url}"
                        )
                    ref = inner[chunk_key]
                    if not (isinstance(ref, list) and len(ref) == 3):
                        raise SourceError(
                            f"unexpected ref format for `{chunk_key}`: {ref!r}"
                        )
                    _src_url, offset, size = ref
                    buf = _range_get_with_retries(
                        sess,
                        target_https,
                        offset,
                        size,
                        what=f"chunk {chunk_key} of {target_https}",
                    )

                    actual_shape = []
                    for d, idx in enumerate(chunk_idx):
                        start = idx * chunks_shape[d]
                        end = min(start + chunks_shape[d], shape[d])
                        actual_shape.append(end - start)
                    expected_bytes = int(np.prod(actual_shape)) * dtype.itemsize
                    if len(buf) != expected_bytes:
                        raise SourceError(
                            f"chunk `{chunk_key}`: got {len(buf)} bytes, "
                            f"expected {expected_bytes} for shape "
                            f"{actual_shape} dtype {dtype}"
                        )
                    chunk_data = np.frombuffer(buf, dtype=dtype).reshape(
                        actual_shape
                    )
                    slices = tuple(
                        slice(
                            idx * chunks_shape[d],
                            idx * chunks_shape[d] + actual_shape[d],
                        )
                        for d, idx in enumerate(chunk_idx)
                    )
                    full[slices] = chunk_data

            zattrs = _parse(inner.get(f"{var_name}/.zattrs", {})) or {}

            # Apply mask_and_scale equivalent: replace ROMS sentinel
            # `_FillValue` (commonly ~1e37 on land cells) with NaN so
            # downstream code sees the same data xarray+zarr would have
            # produced. Without this, the unmasked sentinel values blow up
            # the destagger / regrid pipeline.
            if np.issubdtype(dtype, np.floating):
                fill = zattrs.get("_FillValue")
                if fill is None:
                    fill = zarray.get("fill_value")
                if fill is not None:
                    fill_arr = np.array(fill, dtype=dtype)
                    if not np.isnan(fill_arr):
                        full = np.where(full == fill_arr, np.nan, full)
                # Also handle CF `missing_value` if present alongside _FillValue
                missing = zattrs.get("missing_value")
                if missing is not None:
                    miss_arr = np.array(missing, dtype=dtype)
                    if not np.isnan(miss_arr):
                        full = np.where(full == miss_arr, np.nan, full)
            dims = zattrs.get("_ARRAY_DIMENSIONS")
            if dims is None:
                dims = [f"{var_name}_dim_{i}" for i in range(full.ndim)]
            attrs = {k: v for k, v in zattrs.items() if k != "_ARRAY_DIMENSIONS"}
            return var_name, full, dims, attrs

        # Variables fetched in parallel — typically 2 (u_sur, v_sur) per
        # fhour or 4 (coords + mask + angle) per cycle, so a small inner
        # pool is plenty.
        ds_data: dict = {}
        with _Pool(max_workers=min(4, len(vars_to_pull))) as ex:
            futures = [ex.submit(_fetch_var, v) for v in vars_to_pull]
            for fut in _as_completed(futures):
                name, arr, dims, attrs = fut.result()
                ds_data[name] = (dims, arr, attrs)
        return xr.Dataset(ds_data)

    def _open_and_load(self, s3_uri: str, vars_to_pull: list[str]):
        """Plain h5netcdf+s3fs open — fallback when kerchunk isn't available."""
        import xarray as xr

        if not vars_to_pull:
            raise ValueError("vars_to_pull must be non-empty")
        try:
            ds = xr.open_dataset(
                s3_uri,
                engine="h5netcdf",
                decode_times=False,
                backend_kwargs={"storage_options": {"anon": True}},
            )
        except Exception as e:
            raise SourceError(
                f"failed to open {s3_uri} (h5netcdf/s3fs): {e}"
            ) from e
        try:
            present = [v for v in vars_to_pull if v in ds.variables]
            missing = sorted(set(vars_to_pull) - set(present))
            if missing:
                raise SourceError(
                    f"GoMOFS 2ds at {s3_uri} is missing expected variables: {missing}"
                )
            return ds[present].load()
        finally:
            ds.close()

    # --- Cycle-context builder -------------------------------------------

    def _build_cycle_context(
        self,
        model,
        cycle_dt: datetime,
        bbox,
        static_file: Path,
    ) -> _CycleContext:
        import numpy as np
        import xarray as xr
        from scipy.spatial import cKDTree

        target_lat, target_lon = self._target_grid(bbox, np)
        with xr.open_dataset(static_file, decode_times=False) as ds:
            lon_rho = ds["lon_rho"].values
            lat_rho = ds["lat_rho"].values
            mask_rho = ds["mask_rho"].values
            angle = ds["angle"].values

        rho_shape = lon_rho.shape

        target_lon_2d, target_lat_2d = np.meshgrid(target_lon, target_lat)
        target_pts = np.column_stack(
            [target_lon_2d.ravel(), target_lat_2d.ravel()]
        )

        # Build the rho-grid kdtree from ONLY water cells. This way every
        # target → rho lookup picks a wet cell by construction; narrow
        # channels and small harbors that sub-resolve the rho grid get the
        # nearest reachable water cell instead of being marked land.
        water = mask_rho.ravel() > 0.5
        flat_water_indices = np.where(water)[0]
        if flat_water_indices.size == 0:
            raise SourceError(
                "GoMOFS static file has no water cells in mask_rho — "
                "something is wrong with the upstream data"
            )
        water_pts = np.column_stack(
            [lon_rho.ravel()[flat_water_indices], lat_rho.ravel()[flat_water_indices]]
        )
        water_tree = cKDTree(water_pts)
        distances, water_local_idx = water_tree.query(target_pts, k=1)
        rho_water_indices = flat_water_indices[water_local_idx]

        # GoMOFS-domain sanity check: only mask targets so far from any GoMOFS
        # water cell that they're clearly outside the model's reach. With the
        # generous 0.5° default this is essentially a no-op for typical bboxes;
        # the real "is this on land?" decision is made by the coastline lookup
        # below, not by this distance.
        in_domain = (distances < self.max_dist_to_gomofs_water_deg).reshape(
            target_lat.size, target_lon.size
        )

        # Coastline-based water mask: GSHHG full-resolution (~100 m precision)
        # tells us, per output cell rectangle, whether any of the cell is over
        # water. Cells fully contained in a single land polygon are masked;
        # any cell that grazes water gets a value from its nearest GoMOFS rho
        # cell — so harbors, inlets, and the actual shoreline get preserved
        # even if the GoMOFS rho mask is coarser than the true coast.
        from grib_nomad.core.coastline import CoastlineLookup

        try:
            coastline = CoastlineLookup.get()
            has_water = coastline.has_water_grid(target_lat, target_lon, np)
        except Exception as e:
            log.warning(
                "coastline lookup unavailable (%s); falling back to "
                "GoMOFS-distance-only masking",
                e,
            )
            has_water = np.ones_like(in_domain, dtype=bool)

        over_water_mask = in_domain & has_water

        return _CycleContext(
            cycle_dt=cycle_dt,
            target_lat=target_lat,
            target_lon=target_lon,
            rho_water_indices=rho_water_indices,
            rho_water_distances=distances,
            in_domain=in_domain,
            over_water_mask=over_water_mask,
            angle=angle,
            rho_shape=rho_shape,
        )

    # --- Per-fhour processing --------------------------------------------

    def _process_one_fhour(
        self,
        request: DownloadRequest,
        fhour: int,
        dest_dir: Path,
        cycle_dt: datetime,
        ctx: _CycleContext,
        cycle_refs: dict | None,
    ) -> DownloadedPart:
        import numpy as np
        import xarray as xr

        nc_path = self._fetch_fhour_subset(
            request.model, cycle_dt, fhour, cycle_refs
        )
        u_var = request.model.extra.get("u_var", "u_sur")
        v_var = request.model.extra.get("v_var", "v_sur")

        with xr.open_dataset(nc_path, decode_times=False) as ds:
            u_xi_staggered = ds[u_var].isel(ocean_time=0).values  # (eta_rho, xi_rho - 1)
            v_eta_staggered = ds[v_var].isel(ocean_time=0).values  # (eta_rho - 1, xi_rho)

        # Destagger: u-points lie midway between rho-points along xi; v-points
        # midway along eta. For interior rho cells we average the two adjacent
        # staggered cells; on boundaries we copy the nearest staggered cell.
        u_rho = _u_to_rho(u_xi_staggered, ctx.rho_shape, np)
        v_rho = _v_to_rho(v_eta_staggered, ctx.rho_shape, np)

        # Rotate from ROMS curvilinear (xi/eta) to geographic (east/north)
        # using the rho-grid `angle`. Without this step UOGRD/VOGRD would
        # carry model-frame components and the vectors would render rotated
        # by tens of degrees in routing software.
        cos_a = np.cos(ctx.angle)
        sin_a = np.sin(ctx.angle)
        u_east = u_rho * cos_a - v_rho * sin_a
        v_north = u_rho * sin_a + v_rho * cos_a

        u_grid = u_east.ravel()[ctx.rho_water_indices].reshape(
            ctx.target_lat.size, ctx.target_lon.size
        )
        v_grid = v_north.ravel()[ctx.rho_water_indices].reshape(
            ctx.target_lat.size, ctx.target_lon.size
        )

        # Soft coastal mask: only the value-substitution edge case where the
        # nearest water cell is within `coastal_extension_deg` (default 5 km).
        # Past that, we mask — keeps currents from bleeding kilometres deep
        # into solid land just because some water cell happens to be the
        # nearest in the kdtree. Tighter than the previous 0.5° gate that
        # was producing the over-extension you saw.
        u_grid = np.where(ctx.over_water_mask, u_grid, np.nan)
        v_grid = np.where(ctx.over_water_mask, v_grid, np.nan)
        u_grid = np.where(np.abs(u_grid) > 1000, np.nan, u_grid)
        v_grid = np.where(np.abs(v_grid) > 1000, np.nan, v_grid)

        _, https_url, _ = self._file_paths(request.model.extra, cycle_dt, fhour)
        grib_path = dest_dir / (
            f"gomofs_currents_{cycle_dt.strftime('%Y%m%d')}_"
            f"t{cycle_dt.hour:02d}z_f{fhour:03d}.grb2"
        )
        self._write_grib2(
            grib_path,
            u_grid=u_grid,
            v_grid=v_grid,
            lat_grid=ctx.target_lat,
            lon_grid=ctx.target_lon,
            cycle_dt=cycle_dt,
            fhour=fhour,
            np=np,
        )
        return DownloadedPart(
            path=grib_path,
            model_id=request.model.id,
            category=request.category,
            cycle=cycle_dt,
            fhour=fhour,
            bytes_=grib_path.stat().st_size,
            source_url=https_url,
            bbox=request.bbox.to_dict(),
            variables=["UOGRD", "VOGRD"],
            levels=["surface"],
        )

    # --- Helpers ----------------------------------------------------------

    def _check_bbox_intersects_domain(self, request: DownloadRequest) -> None:
        extra = request.model.extra
        bbox = request.bbox
        try:
            d_lat_min = float(extra["domain_lat_min"])
            d_lat_max = float(extra["domain_lat_max"])
            d_lon_min = float(extra["domain_lon_min"])
            d_lon_max = float(extra["domain_lon_max"])
        except KeyError:
            return
        no_overlap = (
            bbox.lat_max < d_lat_min
            or bbox.lat_min > d_lat_max
            or bbox.lon_max < d_lon_min
            or bbox.lon_min > d_lon_max
        )
        if no_overlap:
            raise SourceError(
                f"bbox {bbox.to_dict()} does not intersect GoMOFS domain "
                f"({d_lat_min}..{d_lat_max}N, {d_lon_min}..{d_lon_max}E)."
            )

    def _target_grid(self, bbox, np):
        lat = np.arange(
            bbox.lat_min,
            bbox.lat_max + self.target_lat_step / 2,
            self.target_lat_step,
        )
        lon = np.arange(
            bbox.lon_min,
            bbox.lon_max + self.target_lon_step / 2,
            self.target_lon_step,
        )
        if len(lat) < 2 or len(lon) < 2:
            raise SourceError(
                f"target grid degenerate for bbox {bbox.to_dict()} at "
                f"{self.target_lat_step}°x{self.target_lon_step}°"
            )
        return lat, lon

    def _write_grib2(
        self,
        path: Path,
        *,
        u_grid,
        v_grid,
        lat_grid,
        lon_grid,
        cycle_dt: datetime,
        fhour: int,
        np,
    ) -> None:
        try:
            from eccodes import (
                codes_grib_new_from_samples,
                codes_release,
                codes_set,
                codes_set_values,
                codes_write,
            )
        except ImportError as e:
            raise _missing_dep("eccodes") from e

        nlat = len(lat_grid)
        nlon = len(lon_grid)
        dlat = abs(float(lat_grid[1] - lat_grid[0]))
        dlon = abs(float(lon_grid[1] - lon_grid[0]))
        lat_min = float(lat_grid[0])
        lat_max = float(lat_grid[-1])
        lon_min = float(lon_grid[0]) % 360
        lon_max = float(lon_grid[-1]) % 360

        u_out = np.flipud(u_grid)
        v_out = np.flipud(v_grid)

        with path.open("wb") as f:
            for arr, param_number in (
                (u_out, _GRIB_PARAM_U_CURRENT),
                (v_out, _GRIB_PARAM_V_CURRENT),
            ):
                gid = codes_grib_new_from_samples("regular_ll_sfc_grib2")
                codes_set(gid, "centre", "kwbc")
                codes_set(gid, "subCentre", 0)
                codes_set(gid, "tablesVersion", 4)
                codes_set(gid, "discipline", _GRIB_DISCIPLINE_OCEAN)
                codes_set(gid, "productDefinitionTemplateNumber", 0)
                codes_set(gid, "parameterCategory", _GRIB_CATEGORY_CURRENTS)
                codes_set(gid, "parameterNumber", param_number)
                codes_set(gid, "typeOfGeneratingProcess", 2)  # forecast
                codes_set(gid, "typeOfFirstFixedSurface", 1)
                codes_set(gid, "scaleFactorOfFirstFixedSurface", 0)
                codes_set(gid, "scaledValueOfFirstFixedSurface", 0)
                codes_set(gid, "dataDate", int(cycle_dt.strftime("%Y%m%d")))
                codes_set(gid, "dataTime", cycle_dt.hour * 100)
                codes_set(gid, "stepUnits", "h")
                codes_set(gid, "forecastTime", fhour)
                codes_set(gid, "Ni", nlon)
                codes_set(gid, "Nj", nlat)
                codes_set(gid, "latitudeOfFirstGridPointInDegrees", lat_max)
                codes_set(gid, "latitudeOfLastGridPointInDegrees", lat_min)
                codes_set(gid, "longitudeOfFirstGridPointInDegrees", lon_min)
                codes_set(gid, "longitudeOfLastGridPointInDegrees", lon_max)
                codes_set(gid, "iDirectionIncrementInDegrees", dlon)
                codes_set(gid, "jDirectionIncrementInDegrees", dlat)
                codes_set(gid, "scanningMode", 0)
                values = arr.astype(np.float64)
                if np.isnan(values).any():
                    codes_set(gid, "bitmapPresent", 1)
                    codes_set(gid, "missingValue", _MISSING_VALUE)
                    values = np.where(np.isnan(values), _MISSING_VALUE, values)
                codes_set_values(gid, values.flatten())
                codes_write(gid, f)
                codes_release(gid)


def _range_get_with_retries(
    sess,
    url: str,
    offset: int,
    size: int,
    *,
    what: str,
    max_attempts: int = 3,
) -> bytes:
    """HTTP Range GET with bounded retry on transient errors.

    S3 occasionally closes idle keep-alive sockets mid-flight (raises
    `RemoteDisconnected`), responds 503 under load, or times out a slow
    Range. None of those mean the data is gone — they just mean retry.
    Without this, a single blip would propagate up and disable the kerchunk
    fast-path for the entire run, forcing every remaining fhour through
    the much slower h5netcdf metadata-walk path. Falling back to h5netcdf
    doesn't even help with network problems (same network) — it just adds
    latency.

    Backoff: 0.5s, 1.5s, 3.0s. After max_attempts we raise
    `_TransientFetchError` so the caller knows this was a network event,
    not a data-layout problem, and can choose to fall through to h5netcdf
    for THIS file without poisoning kerchunk for subsequent files.
    """
    import requests

    backoffs = [0.5, 1.5, 3.0]
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            r = sess.get(
                url,
                headers={"Range": f"bytes={offset}-{offset + size - 1}"},
                timeout=120,
            )
            if r.status_code in (200, 206):
                return r.content
            if r.status_code in (500, 502, 503, 504):
                # Server-side transient — retry-eligible.
                last_exc = SourceError(
                    f"transient HTTP {r.status_code} for {what}"
                )
            else:
                # 4xx / other — not retry-eligible, fail fast as systemic.
                raise SourceError(
                    f"Range fetch {r.status_code} for {what}"
                )
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.Timeout,
        ) as e:
            last_exc = e
        if attempt < max_attempts - 1:
            time.sleep(backoffs[attempt])
    raise _TransientFetchError(
        f"Range fetch failed after {max_attempts} attempts for {what}: "
        f"{last_exc}"
    ) from last_exc


def _substitute_refs_url(refs: dict, new_url: str) -> dict:
    """Take a kerchunk reference dict and return a copy in which every chunk
    reference points at `new_url` instead of the original URL.

    A kerchunk refs dict has shape ``{"version": 1, "refs": {key: value}}``
    where each value is either an inline string/bytes (for small metadata
    that's been folded into the JSON) or a 3-element list ``[url, offset,
    size]`` describing a byte slice of an external file. We only rewrite the
    list-shaped entries; inline metadata is identical across fhours of the
    same cycle and stays put.
    """
    out_refs: dict = {}
    for key, val in refs["refs"].items():
        if isinstance(val, list) and len(val) == 3:
            out_refs[key] = [new_url, val[1], val[2]]
        else:
            out_refs[key] = val
    return {"version": refs.get("version", 1), "refs": out_refs}


def _write_json(obj, dest_tmp: Path) -> None:
    """Atomic-save helper for writing kerchunk JSON to disk."""
    with dest_tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f)


def _save_subset_nc(subset, dest_tmp: Path) -> None:
    """Write a small extracted subset to NetCDF, dropping inherited
    encoding that doesn't apply (silences xarray's `unlimited_dims` warning
    when we pulled only static / coord vars that lack `ocean_time`).
    """
    subset.encoding.pop("unlimited_dims", None)
    unlimited = [d for d in ("ocean_time",) if d in subset.dims]
    subset.to_netcdf(dest_tmp, engine="h5netcdf", unlimited_dims=unlimited)


def _u_to_rho(u_xi, rho_shape: tuple[int, int], np):
    """Move u from u-points (eta_rho, xi_rho-1) onto rho-points (eta_rho, xi_rho).

    NaN-aware: if one of the two adjacent u-points is masked (xarray
    decodes ROMS `_FillValue` to NaN), use the other one rather than
    propagating NaN to the rho cell. Only emit NaN when *both* neighbors
    are land. This gives a one-cell water-side extension along coastlines
    without inventing values where genuinely no data exists.
    """
    eta_rho, xi_rho = rho_shape
    out = np.empty((eta_rho, xi_rho), dtype=u_xi.dtype)

    left = u_xi[:, :-1]
    right = u_xi[:, 1:]
    avg = 0.5 * (left + right)  # NaN if either side is NaN
    interior = np.where(
        np.isnan(left),
        right,
        np.where(np.isnan(right), left, avg),
    )
    out[:, 1:-1] = interior
    out[:, 0] = u_xi[:, 0]
    out[:, -1] = u_xi[:, -1]
    return out


def _v_to_rho(v_eta, rho_shape: tuple[int, int], np):
    """Move v from v-points (eta_rho-1, xi_rho) onto rho-points (eta_rho, xi_rho).

    Same NaN-aware fallback as `_u_to_rho`.
    """
    eta_rho, xi_rho = rho_shape
    out = np.empty((eta_rho, xi_rho), dtype=v_eta.dtype)

    bottom = v_eta[:-1, :]
    top = v_eta[1:, :]
    avg = 0.5 * (bottom + top)
    interior = np.where(
        np.isnan(bottom),
        top,
        np.where(np.isnan(top), bottom, avg),
    )
    out[1:-1, :] = interior
    out[0, :] = v_eta[0, :]
    out[-1, :] = v_eta[-1, :]
    return out
