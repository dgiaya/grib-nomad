"""Coastline-based "is this output cell on land?" mask using GSHHG full-resolution.

GSHHG (Global Self-consistent Hierarchical High-resolution Geography Database)
is the standard high-fidelity global shoreline dataset, distributed as
shapefiles by SOEST. Full resolution = ~100 m precision. We only need level 1
(continents and large islands — Cape Cod, Naushon, Martha's Vineyard, etc.
are all there); deeper levels (lakes, ponds) don't matter for ocean routing.

The flow:

  1. On first use we download `gshhg-shp-X.Y.Z.zip` (~45 MB) from SOEST and
     extract `GSHHS_shp/f/GSHHS_f_L1.shp` into the user cache. Subsequent
     runs reuse the extracted shapefile.
  2. We parse the shapefile with `pyshp`, build `shapely` polygons, and stash
     them behind an `STRtree` for O(log n) bounding-box-keyed lookups.
  3. For each output grid cell, we ask: "is this rectangle entirely contained
     in any single GSHHG land polygon?" — if yes, the cell is fully on land
     and gets masked; otherwise (any water touches the cell) we keep it and
     populate from the nearest GoMOFS water rho cell upstream.

The loader is process-cached via a module-level singleton so a single
`grib-nomad download` run only pays the parse cost once even when multiple
GoMOFS recipes share a process.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from platformdirs import user_cache_path

log = logging.getLogger(__name__)

GSHHG_VERSION = "2.3.7"
GSHHG_URL = (
    f"https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-shp-{GSHHG_VERSION}.zip"
)
GSHHG_L1_RELPATH = "GSHHS_shp/f/GSHHS_f_L1.shp"

# Bump when the on-disk pickle structure changes so old pickles get rebuilt.
_PICKLE_FORMAT_VERSION = 1
# Bump when the per-grid mask cache format changes so old masks get rebuilt
# independently of the polygon pickle.
_MASK_FORMAT_VERSION = 1

_LOAD_LOCK = threading.Lock()


def gshhg_root() -> Path:
    return Path(user_cache_path("grib_nomad", appauthor=False)) / "gshhg" / GSHHG_VERSION


def _ensure_gshhg_l1_full() -> Path:
    """Download + extract GSHHG full-resolution L1 shapefile into the user cache."""
    root = gshhg_root()
    target = root / GSHHG_L1_RELPATH
    if target.exists():
        return target

    root.mkdir(parents=True, exist_ok=True)
    zip_path = root / f"gshhg-shp-{GSHHG_VERSION}.zip"
    log.info(
        "Downloading GSHHG full-resolution coastlines (~45 MB, one-time) from %s",
        GSHHG_URL,
    )
    urllib.request.urlretrieve(GSHHG_URL, zip_path)

    log.info("Extracting GSHHG into %s", root)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(root)
    try:
        zip_path.unlink()
    except OSError:
        pass

    if not target.exists():
        raise RuntimeError(
            f"GSHHG L1 full-resolution shapefile missing after extract: {target}"
        )
    return target


def _pickle_path(shp_path: Path) -> Path:
    """Pickle filename pinned to the shapely version we built it with — if
    you upgrade shapely, the pickle is silently rebuilt rather than risking
    a deserialization crash."""
    import shapely

    safe_ver = shapely.__version__.replace(".", "_")
    return shp_path.with_name(
        f"{shp_path.stem}.fmt{_PICKLE_FORMAT_VERSION}.shapely{safe_ver}.pkl"
    )


def _mask_cache_path(target_lat, target_lon) -> Path:
    """Per-grid mask cache path, keyed on the exact bytes of the target
    grid arrays plus the GSHHG dataset version. Any run that produces an
    identical (target_lat, target_lon) — i.e. same bbox + same step —
    reuses the cached boolean mask. Stored as plain .npy alongside the
    GSHHG shapefile so `rm -rf` of the cache dir cleans both."""
    h = hashlib.sha1()
    # `tobytes()` includes dtype/shape implicitly via the buffer length;
    # casting to float64 first means a 0.01° lat array from a 32-bit
    # bbox calculation hashes the same as one from 64-bit.
    h.update(target_lat.astype("float64").tobytes())
    h.update(b"|")
    h.update(target_lon.astype("float64").tobytes())
    digest = h.hexdigest()[:16]
    return (
        gshhg_root()
        / f"mask.gshhg{GSHHG_VERSION}.fmt{_MASK_FORMAT_VERSION}.{digest}.npy"
    )


def _save_mask_cache(cache_path: Path, mask, np) -> None:
    """Best-effort atomic write of the per-grid mask. Failures are logged
    and swallowed — the caller has the in-memory mask either way; a cache
    miss next run is fine.

    Note: `np.save` auto-appends `.npy` to a string/Path path if it
    doesn't already end in `.npy`, which would silently break the
    `.part` → final rename. Passing an open file handle bypasses that
    extension fixup so the bytes land exactly where we expect.
    """
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(cache_path.suffix + ".part")
        with tmp.open("wb") as f:
            np.save(f, mask)
        tmp.replace(cache_path)
        log.info("Wrote GSHHG mask cache %s", cache_path.name)
    except Exception as e:
        log.warning("Failed to cache GSHHG mask (%s)", e)


class CoastlineLookup:
    """STRtree-backed land-polygon lookup for rectangle-vs-coast queries.

    First time we're constructed in a fresh cache: parses ~190K GSHHG L1
    polygons from the shapefile (~5 s), builds an STRtree, and pickles both
    so the next process can load them in well under a second.
    """

    _singleton: CoastlineLookup | None = None

    def __init__(self):
        shp_path = _ensure_gshhg_l1_full()
        pkl_path = _pickle_path(shp_path)

        if pkl_path.exists():
            try:
                t0 = time.time()
                with pkl_path.open("rb") as f:
                    self.polygons, self.tree = pickle.load(f)
                log.info(
                    "GSHHG L1 loaded from pickle: %d polygons in %.2fs (%s)",
                    len(self.polygons),
                    time.time() - t0,
                    pkl_path.name,
                )
                return
            except Exception as e:
                log.warning(
                    "GSHHG pickle load failed (%s); rebuilding from shapefile",
                    e,
                )

        # Cold path: parse shapefile and build STRtree
        import shapefile  # pyshp
        import shapely.geometry as sg
        from shapely.strtree import STRtree

        log.info("Parsing GSHHG L1 shapefile %s (one-time, ~5s)", shp_path)
        t0 = time.time()
        reader = shapefile.Reader(str(shp_path))
        polygons: list = []
        for shape in reader.iterShapes():
            try:
                poly = sg.shape(shape.__geo_interface__)
            except Exception:
                continue
            if poly.is_empty:
                continue
            polygons.append(poly)
        reader.close()

        self.polygons = polygons
        self.tree = STRtree(polygons)
        log.info(
            "GSHHG L1 ready: %d polygons indexed in %.2fs",
            len(polygons),
            time.time() - t0,
        )

        # Pickle for next process
        try:
            tmp = pkl_path.with_suffix(pkl_path.suffix + ".part")
            with tmp.open("wb") as f:
                pickle.dump(
                    (polygons, self.tree),
                    f,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            tmp.replace(pkl_path)
            log.info("Wrote GSHHG pickle %s", pkl_path.name)
        except Exception as e:
            log.warning("Failed to pickle GSHHG (%s) — will reparse next run", e)

    @classmethod
    def get(cls) -> CoastlineLookup:
        with _LOAD_LOCK:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    def has_water_grid(self, target_lat, target_lon, np):
        """For each output cell, return True if its centroid is over water.

        Semantic note: this is point-in-polygon on the cell centroid, not
        rectangle-in-polygon over the full cell box. At GoMOFS-native
        target steps (~0.01° ≈ 1 km) vs GSHHG full-resolution coastlines
        (~100 m), the difference is at most a one-cell border effect
        along shorelines — handled downstream by the nearest-GoMOFS-water
        fill. The earlier rectangle-contains semantic was orders of
        magnitude slower (each predicate eval ran full geometric
        containment against coast polygons with millions of vertices);
        point-in-polygon uses prepared-geometry ray casting in O(log V)
        per point and is genuinely vectorized through GEOS.

        Cached on disk per (target grid bytes, GSHHG version) so any
        rerun with the same grid is an instant numpy load.
        """
        import shapely

        nlat = len(target_lat)
        nlon = len(target_lon)

        cache_path = _mask_cache_path(target_lat, target_lon)
        if cache_path.exists():
            try:
                t0 = time.time()
                mask = np.load(cache_path)
                if mask.shape == (nlat, nlon) and mask.dtype == np.bool_:
                    log.info(
                        "GSHHG mask loaded from cache: %dx%d in %.2fs (%s)",
                        nlat, nlon, time.time() - t0, cache_path.name,
                    )
                    return mask
                log.warning(
                    "GSHHG mask cache shape/dtype mismatch (got %s %s, expected (%d,%d) bool); rebuilding",
                    mask.shape, mask.dtype, nlat, nlon,
                )
            except Exception as e:
                log.warning("GSHHG mask cache load failed (%s); rebuilding", e)

        # Cell centroids as flat lon/lat arrays. `shapely.contains_xy`
        # consumes raw arrays — no need to construct shapely Point objects.
        t0 = time.time()
        lon_grid, lat_grid = np.meshgrid(target_lon, target_lat)
        lon_flat = lon_grid.ravel()
        lat_flat = lat_grid.ravel()

        # Bbox-prune candidate land polygons via the prebuilt class-level
        # STRtree. This is the only step that touches all 188K GSHHG
        # polygons; for typical regional bboxes it shrinks the working
        # set to a few thousand at most.
        grid_box = shapely.box(
            float(lon_flat.min()), float(lat_flat.min()),
            float(lon_flat.max()), float(lat_flat.max()),
        )
        candidate_indices = list(self.tree.query(grid_box))
        if not candidate_indices:
            # Nothing on this grid touches land — every cell is water.
            has_water = np.ones((nlat, nlon), dtype=bool)
            log.info(
                "GSHHG mask: no candidate land polygons in bbox; all-water mask (%dx%d) in %.2fs",
                nlat, nlon, time.time() - t0,
            )
            _save_mask_cache(cache_path, has_water, np)
            return has_water

        # Vectorized point-in-polygon: one C call per candidate polygon,
        # OR-aggregated. `contains_xy` uses GEOS prepared geometries
        # internally, so even big polygons (continent boundaries) are
        # cheap per-point after the first call. Bail out the moment every
        # cell is already classified as land — common when the bbox sits
        # mostly inside a continent.
        on_land = np.zeros(lon_flat.size, dtype=bool)
        for idx in candidate_indices:
            if on_land.all():
                break
            on_land |= shapely.contains_xy(self.polygons[idx], lon_flat, lat_flat)

        has_water = (~on_land).reshape(nlat, nlon)
        log.info(
            "GSHHG mask built: %dx%d cells in %.2fs (%d candidate polygons, %d land cells)",
            nlat, nlon, time.time() - t0, len(candidate_indices), int(on_land.sum()),
        )
        _save_mask_cache(cache_path, has_water, np)
        return has_water
