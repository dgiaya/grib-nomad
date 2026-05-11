"""On-disk cache for source-fetched files (raw model files, .idx files, etc.).

Cache layout
------------

    <cache_root>/<source>/<model_id>/<YYYYMMDDHH>/<filename>

  - `<cache_root>` lives under `platformdirs.user_cache_path('grib_nomad')`.
  - `<source>` is e.g. 'nomads', 'gomofs'.
  - `<model_id>` is e.g. 'hrrr_conus_sfc'.
  - `<YYYYMMDDHH>` encodes the cycle init time in UTC (date + hour).
  - `<filename>` is whatever the source named the artifact.

Eviction policy
---------------

After every cache touch we run a sweep that deletes any cycle directory whose
forecast horizon has fully passed (`cycle + max_fhour < now`). This is the
"only keep future-relevant data" rule. We never prune by size; if disk pressure
becomes an issue we can layer LRU on top later.

Invariants
----------

  - Files in the cache are content-immutable: a published cycle's data does not
    change. Cache key is therefore (source, model_id, cycle_dt, filename) and a
    presence check is enough — no need for hashes or ETags.
  - Writers must use `atomic_save()` so partial downloads don't poison the cache.
"""

from __future__ import annotations

import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from platformdirs import user_cache_path

_CACHE_LOCK = threading.Lock()


def cache_root() -> Path:
    p = user_cache_path("grib_nomad", appauthor=False, ensure_exists=True)
    return Path(p)


def cycle_dir(
    source: str, model_id: str, cycle_dt: datetime
) -> Path:
    """Directory holding all cached artifacts for one model+cycle."""
    if cycle_dt.tzinfo is None:
        raise ValueError("cycle_dt must be timezone-aware")
    cycle_utc = cycle_dt.astimezone(timezone.utc)
    return (
        cache_root()
        / source
        / model_id
        / cycle_utc.strftime("%Y%m%d%H")
    )


def cached_path(
    source: str,
    model_id: str,
    cycle_dt: datetime,
    filename: str,
) -> Path:
    return cycle_dir(source, model_id, cycle_dt) / filename


def is_cached(
    source: str,
    model_id: str,
    cycle_dt: datetime,
    filename: str,
    *,
    min_size: int = 1,
) -> bool:
    """Cache hit only if the file exists and is at least `min_size` bytes."""
    p = cached_path(source, model_id, cycle_dt, filename)
    try:
        return p.is_file() and p.stat().st_size >= min_size
    except OSError:
        return False


def model_dir(source: str, model_id: str) -> Path:
    """Directory holding cycle-independent artifacts for a model.

    Used for kerchunk reference templates that describe the model's HDF5
    chunk layout, which is identical across all forecast cycles a given
    OFS/NWP model publishes — walk once, reuse forever.
    """
    return cache_root() / source / model_id


def model_cached_path(source: str, model_id: str, filename: str) -> Path:
    return model_dir(source, model_id) / filename


def is_model_cached(
    source: str, model_id: str, filename: str, *, min_size: int = 1
) -> bool:
    p = model_cached_path(source, model_id, filename)
    try:
        return p.is_file() and p.stat().st_size >= min_size
    except OSError:
        return False


def atomic_save(target: Path, write_fn) -> Path:
    """Run `write_fn(tmp_path)` against a temp file and rename into place atomically.

    Ensures partial writes (interrupted downloads, OOM, kill -9) never leave a
    corrupted file at `target`. The rename is atomic on POSIX; if the target
    already exists we leave the existing one alone (cache hit racing with us).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        write_fn(tmp)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if target.exists():
        # Someone else won the race; drop our temp.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return target
    tmp.replace(target)
    return target


def prune_stale(
    *,
    now: datetime | None = None,
    max_horizon_hours: dict[tuple[str, str], int] | None = None,
) -> int:
    """Delete cached cycle directories whose forecast horizon has fully passed.

    `max_horizon_hours[(source, model_id)]` tells us how far past `cycle_dt` a
    given model's forecast extends; once `cycle_dt + horizon < now`, all data in
    that cycle directory is in the past and we drop it. Models without an entry
    fall back to a generous default (10 days) so we don't accidentally evict
    cycles for models we don't recognize.

    Returns the number of cycle directories removed.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    horizons = max_horizon_hours or {}
    removed = 0
    root = cache_root()
    if not root.exists():
        return 0
    with _CACHE_LOCK:
        for source_dir in root.iterdir():
            if not source_dir.is_dir():
                continue
            source = source_dir.name
            for model_dir in source_dir.iterdir():
                if not model_dir.is_dir():
                    continue
                model_id = model_dir.name
                horizon = horizons.get((source, model_id), 24 * 10)
                for cycle_dir_path in model_dir.iterdir():
                    if not cycle_dir_path.is_dir():
                        continue
                    try:
                        cycle_dt = datetime.strptime(
                            cycle_dir_path.name, "%Y%m%d%H"
                        ).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if cycle_dt + timedelta(hours=horizon) < now:
                        shutil.rmtree(cycle_dir_path, ignore_errors=True)
                        removed += 1
    return removed


def clear_all() -> None:
    """Wipe the entire cache. Mostly for tests / debugging."""
    root = cache_root()
    if root.exists():
        shutil.rmtree(root)
