"""Time interpolation of GRIB2 message streams.

The runner builds a "master timeline" — the union of every category's native
valid_times within the recipe window. At every master timestep we want every
category present, so categories whose native cadence is sparser than the master
get linearly interpolated between bracketing natives.

We use eccodes directly so we can clone source messages — this preserves the
original grid (lat/lon, Lambert Conformal, whatever), parameter codes, and
local definitions, then we just update `forecastTime`/`dataDate`/`dataTime`
and the values array. No regridding, no parameter remapping.

Times outside the native range are skipped (no extrapolation). Direction
fields (parameter numbers WMO uses for wave-direction-style angles) are
interpolated as unit vectors and converted back, since linear interpolation of
degrees would average 350° + 10° as 180° instead of 0°.

The complete API surface here is `interpolate_to_timeline()`; everything else
is internal.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from grib_nomad.core.combine import DownloadedPart


# WMO GRIB2 parameter codes for direction-style fields (degrees, [0, 360)).
# (discipline, parameterCategory, parameterNumber)
DIRECTION_PARAMS: frozenset[tuple[int, int, int]] = frozenset(
    {
        (0, 2, 0),  # Wind direction (atmosphere)
        (10, 0, 4),  # Direction of wind waves
        (10, 0, 7),  # Direction of swell waves
        (10, 0, 10),  # Primary wave direction
        (10, 0, 14),  # Secondary wave direction of swell waves
        (10, 1, 0),  # Current direction (true)
    }
)


@dataclass
class _Message:
    """One decoded GRIB2 message held in memory while we pick brackets."""

    valid_time: datetime
    cycle_dt: datetime
    fhour: int
    sig: tuple
    is_direction: bool
    values: Any  # numpy ndarray, possibly with NaN where missing
    ni: int
    nj: int
    has_bitmap: bool
    handle_for_clone: int  # eccodes gid; released after we're done with this group


def interpolate_to_timeline(
    parts: list[DownloadedPart],
    target_times: list[datetime],
    out_path: Path,
    *,
    log_warning=lambda _m: None,
) -> tuple[DownloadedPart, list[datetime]]:
    """Read every GRIB2 message in `parts`, time-interpolate per (param, grid)
    onto `target_times`, write the result as a single GRIB2 file at `out_path`.

    Returns (combined DownloadedPart describing the output file, list of
    target_times that ended up with at least one message — i.e. that fell
    within at least one parameter group's native time range). Times outside
    every group's range are silently dropped from the second list.
    """
    try:
        import numpy as np
        from eccodes import (
            codes_clone,
            codes_get_long,
            codes_get_values,
            codes_grib_new_from_file,
            codes_release,
            codes_set,
            codes_set_values,
            codes_write,
        )
    except ImportError as e:
        raise RuntimeError(
            f"interpolation requires `eccodes` and `numpy` "
            f"(install via the [gomofs] extra) — missing: {e.name}"
        ) from e

    if not parts:
        raise ValueError("interpolate_to_timeline called with no parts")

    # 1. Read every message from every part, group by (parameter, grid) signature.
    by_sig: dict[tuple, list[_Message]] = defaultdict(list)
    handles_to_release: list[int] = []
    target_times = sorted(set(target_times))

    for part in parts:
        with part.path.open("rb") as f:
            while True:
                gid = codes_grib_new_from_file(f)
                if gid is None:
                    break
                handles_to_release.append(gid)
                msg = _decode(gid, np, codes_get_long, codes_get_values)
                by_sig[msg.sig].append(msg)

    if not by_sig:
        raise ValueError(f"no GRIB2 messages found in {len(parts)} parts")

    # 2. For each group, sort by valid_time and bracket-interpolate to target_times.
    achieved_times: set[datetime] = set()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    n_messages_written = 0
    with out_path.open("wb") as out_f:
        for sig, msgs in by_sig.items():
            msgs.sort(key=lambda m: m.valid_time)
            valid_times = [m.valid_time for m in msgs]

            for target in target_times:
                produced = _produce_message(
                    msgs,
                    valid_times,
                    target,
                    out_f=out_f,
                    np=np,
                    codes_clone=codes_clone,
                    codes_set=codes_set,
                    codes_set_values=codes_set_values,
                    codes_write=codes_write,
                    codes_release=codes_release,
                    codes_get_long=codes_get_long,
                )
                if produced:
                    achieved_times.add(target)
                    n_messages_written += 1

        out_f.flush()
        total_bytes = out_f.tell()

    # 3. Release source handles
    for gid in handles_to_release:
        codes_release(gid)

    if not achieved_times:
        log_warning(
            f"  interpolation produced 0 messages — every target time fell "
            f"outside every parameter's native range"
        )

    # Build a representative DownloadedPart describing the combined file.
    sample = parts[0]
    combined_part = DownloadedPart(
        path=out_path,
        model_id="+".join(sorted({p.model_id for p in parts})),
        category=sample.category,
        cycle=min(p.cycle for p in parts),
        fhour=-1,  # composite: not a single fhour
        bytes_=total_bytes,
        source_url=";".join(sorted({p.source_url for p in parts})[:3]),
        bbox=sample.bbox,
        variables=sorted({v for p in parts for v in p.variables}),
        levels=sorted({lv for p in parts for lv in p.levels}),
    )
    return combined_part, sorted(achieved_times)


# --- internals -------------------------------------------------------------


def _decode(gid: int, np, codes_get_long, codes_get_values) -> _Message:
    """Pull just enough metadata off a GRIB2 handle to bracket-interpolate it."""
    discipline = int(codes_get_long(gid, "discipline"))
    param_cat = int(codes_get_long(gid, "parameterCategory"))
    param_num = int(codes_get_long(gid, "parameterNumber"))
    type_lev = int(codes_get_long(gid, "typeOfFirstFixedSurface"))
    scaled_lev = int(codes_get_long(gid, "scaledValueOfFirstFixedSurface"))
    ni = int(codes_get_long(gid, "Ni"))
    nj = int(codes_get_long(gid, "Nj"))
    grid_template = int(codes_get_long(gid, "gridDefinitionTemplateNumber"))
    sig = (discipline, param_cat, param_num, type_lev, scaled_lev, ni, nj, grid_template)

    data_date = int(codes_get_long(gid, "dataDate"))
    data_time = int(codes_get_long(gid, "dataTime"))
    fhour = int(codes_get_long(gid, "forecastTime"))
    cycle_dt = datetime(
        year=data_date // 10000,
        month=(data_date // 100) % 100,
        day=data_date % 100,
        hour=data_time // 100,
        minute=data_time % 100,
        tzinfo=timezone.utc,
    )
    valid = cycle_dt + timedelta(hours=fhour)

    values = codes_get_values(gid).astype(np.float64).reshape(nj, ni)
    has_bitmap = False
    try:
        has_bitmap = bool(codes_get_long(gid, "bitmapPresent"))
    except Exception:
        has_bitmap = False
    if has_bitmap:
        # `missingValue` is a double, not a long — read it raw via codes_get
        from eccodes import codes_get as _codes_get  # local import keeps decode signature simple

        try:
            miss = float(_codes_get(gid, "missingValue"))
            values = np.where(values == miss, np.nan, values)
        except Exception:
            pass

    return _Message(
        valid_time=valid,
        cycle_dt=cycle_dt,
        fhour=fhour,
        sig=sig,
        is_direction=(discipline, param_cat, param_num) in DIRECTION_PARAMS,
        values=values,
        ni=ni,
        nj=nj,
        has_bitmap=has_bitmap,
        handle_for_clone=gid,
    )


def _produce_message(
    msgs: list[_Message],
    valid_times: list[datetime],
    target: datetime,
    *,
    out_f,
    np,
    codes_clone,
    codes_set,
    codes_set_values,
    codes_write,
    codes_release,
    codes_get_long,
) -> bool:
    """Emit one GRIB2 message at `target` (native pass-through or interpolated).

    Returns True if a message was written, False if `target` is outside the
    natively-covered range for this parameter group.
    """
    # Exact native hit?
    if target in valid_times:
        idx = valid_times.index(target)
        src = msgs[idx]
        new_gid = codes_clone(src.handle_for_clone)
        try:
            _set_values_with_bitmap(new_gid, src.values, np, codes_set, codes_set_values)
            codes_write(new_gid, out_f)
        finally:
            codes_release(new_gid)
        return True

    # Bracketed interpolation
    i = bisect.bisect_left(valid_times, target)
    if i == 0 or i == len(valid_times):
        return False  # no extrapolation

    t0, t1 = valid_times[i - 1], valid_times[i]
    span = (t1 - t0).total_seconds()
    if span <= 0:
        return False
    w = (target - t0).total_seconds() / span
    src0 = msgs[i - 1]
    src1 = msgs[i]

    if src0.is_direction:
        # Linear interp on (sin, cos) of the angle, then atan2 back.
        # Preserves NaN where either side is missing.
        d0 = np.deg2rad(src0.values)
        d1 = np.deg2rad(src1.values)
        u = (1 - w) * np.sin(d0) + w * np.sin(d1)
        v = (1 - w) * np.cos(d0) + w * np.cos(d1)
        interp = (np.rad2deg(np.arctan2(u, v)) + 360.0) % 360.0
        # propagate NaN
        nanmask = np.isnan(src0.values) | np.isnan(src1.values)
        interp = np.where(nanmask, np.nan, interp)
    else:
        nanmask = np.isnan(src0.values) | np.isnan(src1.values)
        interp = np.where(nanmask, np.nan, (1 - w) * src0.values + w * src1.values)

    # Clone the earlier message, update time + values
    new_gid = codes_clone(src0.handle_for_clone)
    try:
        # forecastTime is reckoned from src0's cycle. Use src0 because cloning it
        # carries its dataDate/dataTime and grid; we just need to point the
        # forecastTime to land on `target`.
        new_fhour_seconds = (target - src0.cycle_dt).total_seconds()
        new_fhour = int(round(new_fhour_seconds / 3600.0))
        codes_set(new_gid, "forecastTime", new_fhour)
        _set_values_with_bitmap(new_gid, interp, np, codes_set, codes_set_values)
        codes_write(new_gid, out_f)
    finally:
        codes_release(new_gid)
    return True


_GRIB_MISSING = 9.999e20


def _set_values_with_bitmap(gid, arr, np, codes_set, codes_set_values) -> None:
    """Encode an array possibly containing NaN as a GRIB2 message with bitmap."""
    flat = arr.flatten()
    if np.isnan(flat).any():
        codes_set(gid, "bitmapPresent", 1)
        codes_set(gid, "missingValue", _GRIB_MISSING)
        flat = np.where(np.isnan(flat), _GRIB_MISSING, flat)
    codes_set_values(gid, flat.astype(np.float64))
