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
        """For each output cell, return True if any of the cell rectangle is over water.

        A cell is "fully on land" iff some single GSHHG L1 polygon
        contains its entire bounding rectangle; otherwise it's marked as
        having water (so a coastal cell that's mostly Cape Cod but has a
        sliver of Buzzards Bay still counts as water and gets a current
        value from the nearest GoMOFS rho cell).
        """
        import shapely.geometry as sg

        nlat = len(target_lat)
        nlon = len(target_lon)
        # Cell side lengths (assume regular spacing)
        dlat = float(target_lat[1] - target_lat[0]) if nlat > 1 else 0.01
        dlon = float(target_lon[1] - target_lon[0]) if nlon > 1 else 0.01

        # Build a quick coarse-cull bbox over the whole target grid so we don't
        # iterate every continent on the planet for each cell.
        grid_box = sg.box(
            float(target_lon.min()) - dlon,
            float(target_lat.min()) - dlat,
            float(target_lon.max()) + dlon,
            float(target_lat.max()) + dlat,
        )
        candidate_indices = list(self.tree.query(grid_box))
        if not candidate_indices:
            # No land polygons touch the grid at all: every cell is water.
            return np.ones((nlat, nlon), dtype=bool)
        candidate_polys = [self.polygons[i] for i in candidate_indices]

        from shapely.strtree import STRtree as _STRtree

        local_tree = _STRtree(candidate_polys)

        has_water = np.ones((nlat, nlon), dtype=bool)
        for i in range(nlat):
            lat = float(target_lat[i])
            for j in range(nlon):
                lon = float(target_lon[j])
                cell = sg.box(
                    lon - dlon / 2,
                    lat - dlat / 2,
                    lon + dlon / 2,
                    lat + dlat / 2,
                )
                hits = local_tree.query(cell)
                if len(hits) == 0:
                    continue  # no land touches this cell — all water
                fully_on_land = False
                for idx in hits:
                    if candidate_polys[idx].contains(cell):
                        fully_on_land = True
                        break
                if fully_on_land:
                    has_water[i, j] = False
        return has_water
