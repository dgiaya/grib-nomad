"""End-to-end runner smoke test against an in-memory FakeSource that produces
real (minimal) GRIB2 messages via eccodes — enough for the interpolation step
to read them back, time-interpolate, and concatenate into a combined GRIB2.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from grib_nomad.core.combine import DownloadedPart
from grib_nomad.core.recipe import CategoryPlan, ModelTier, Recipe
from grib_nomad.core.runner import run_recipe
from grib_nomad.models import load_registry
from grib_nomad.sources.base import DownloadRequest, Source


def _write_minimal_grib2(
    path: Path,
    *,
    cycle_dt: datetime,
    fhour: int,
    discipline: int,
    param_cat: int,
    param_num: int,
    value: float,
) -> None:
    """Write one GRIB2 message at a tiny regular lat/lon grid with a constant value."""
    import numpy as np
    from eccodes import (
        codes_grib_new_from_samples,
        codes_release,
        codes_set,
        codes_set_values,
        codes_write,
    )

    ni, nj = 5, 4
    gid = codes_grib_new_from_samples("regular_ll_sfc_grib2")
    codes_set(gid, "centre", "kwbc")
    codes_set(gid, "discipline", discipline)
    codes_set(gid, "parameterCategory", param_cat)
    codes_set(gid, "parameterNumber", param_num)
    codes_set(gid, "typeOfFirstFixedSurface", 1)
    codes_set(gid, "scaleFactorOfFirstFixedSurface", 0)
    codes_set(gid, "scaledValueOfFirstFixedSurface", 0)
    codes_set(gid, "dataDate", int(cycle_dt.strftime("%Y%m%d")))
    codes_set(gid, "dataTime", cycle_dt.hour * 100)
    codes_set(gid, "forecastTime", fhour)
    codes_set(gid, "Ni", ni)
    codes_set(gid, "Nj", nj)
    codes_set(gid, "latitudeOfFirstGridPointInDegrees", 45.0)
    codes_set(gid, "longitudeOfFirstGridPointInDegrees", 285.0)
    codes_set(gid, "latitudeOfLastGridPointInDegrees", 42.0)
    codes_set(gid, "longitudeOfLastGridPointInDegrees", 289.0)
    codes_set(gid, "iDirectionIncrementInDegrees", 1.0)
    codes_set(gid, "jDirectionIncrementInDegrees", 1.0)
    codes_set(gid, "scanningMode", 0)
    codes_set_values(gid, np.full(ni * nj, value, dtype=np.float64))
    with path.open("wb") as f:
        codes_write(gid, f)
    codes_release(gid)


class FakeSource(Source):
    name = "nomads"

    def __init__(self):
        self.calls: list[DownloadRequest] = []

    def download(
        self,
        request: DownloadRequest,
        dest_dir: Path,
        *,
        on_part=None,
        on_status=None,
    ) -> list[DownloadedPart]:
        self.calls.append(request)
        parts: list[DownloadedPart] = []
        cycle_dt = datetime.combine(
            request.cycle_date, datetime.min.time()
        ).replace(hour=request.cycle_hour, tzinfo=timezone.utc)
        # Use the first variable's category to vary the parameter codes a bit
        cat = request.model.categories[request.category]
        for fh in request.fhours:
            p = (
                dest_dir
                / f"{request.model.id}_{request.category}_f{fh:03d}.grb2"
            )
            # Write one message per declared variable
            with p.open("wb") as out:
                pass  # truncate
            tmp_paths = []
            for v_idx, var_name in enumerate(cat.vars):
                tmp = dest_dir / f"_tmp_{request.model.id}_{request.category}_{fh}_{v_idx}.grb2"
                _write_minimal_grib2(
                    tmp,
                    cycle_dt=cycle_dt,
                    fhour=fh,
                    discipline=0,
                    param_cat=2,
                    param_num=2 + v_idx,  # bump so each var is distinct
                    value=float(fh + v_idx * 0.1),
                )
                tmp_paths.append(tmp)
            with p.open("wb") as out:
                for tp in tmp_paths:
                    out.write(tp.read_bytes())
                    tp.unlink()
            part = DownloadedPart(
                path=p,
                model_id=request.model.id,
                category=request.category,
                cycle=cycle_dt,
                fhour=fh,
                bytes_=p.stat().st_size,
                source_url=f"fake://{request.model.id}/f{fh:03d}",
                bbox=request.bbox.to_dict(),
                variables=list(cat.vars),
                levels=list(cat.levels),
            )
            parts.append(part)
            if on_part is not None:
                on_part(part)
        return parts


def test_runner_dispatch_and_master_timeline(tmp_path: Path):
    """Two-tier wind recipe: HRRR for first 6h, GFS for the rest of 12h.
    Verifies tier dispatch, master-timeline construction, and end-to-end output.
    """
    registry = load_registry()
    fake = FakeSource()
    start_dt = datetime(2026, 5, 8, 18, tzinfo=timezone.utc)
    now = start_dt + timedelta(minutes=30)

    recipe = Recipe(
        name="smoke",
        region="gulf-of-maine-wide",
        duration_hours=12,
        start=start_dt,
        categories=[
            CategoryPlan(
                category="wind",
                tiers=[
                    ModelTier("hrrr_conus_sfc", until_hours=6),
                    ModelTier("gfs_0p25"),
                ],
            ),
        ],
    )
    result = run_recipe(
        recipe,
        registry=registry,
        sources={"nomads": fake},
        dest_dir=tmp_path,
        quiet=True,
        now=now,
    )

    # Two tiers should have produced two requests
    assert len(fake.calls) == 2
    by_model = {req.model.id: req for req in fake.calls}
    assert "hrrr_conus_sfc" in by_model
    assert "gfs_0p25" in by_model

    # HRRR tier covers fhours whose valid_time is in [start, start+6h]
    # (start_dt itself is inclusive — first hour of the window is in scope).
    hrrr_validation = by_model["hrrr_conus_sfc"]
    hrrr_cycle = datetime.combine(
        hrrr_validation.cycle_date, datetime.min.time()
    ).replace(hour=hrrr_validation.cycle_hour, tzinfo=timezone.utc)
    for fh in hrrr_validation.fhours:
        valid = hrrr_cycle + timedelta(hours=fh)
        assert start_dt <= valid <= start_dt + timedelta(hours=6)

    # GFS picks up after HRRR
    gfs_validation = by_model["gfs_0p25"]
    gfs_cycle = datetime.combine(
        gfs_validation.cycle_date, datetime.min.time()
    ).replace(hour=gfs_validation.cycle_hour, tzinfo=timezone.utc)
    last_hrrr_valid = max(
        hrrr_cycle + timedelta(hours=fh) for fh in hrrr_validation.fhours
    )
    for fh in gfs_validation.fhours:
        valid = gfs_cycle + timedelta(hours=fh)
        assert valid > last_hrrr_valid
        assert valid <= start_dt + timedelta(hours=12)

    # Output exists and is non-empty
    assert result.output_grib.exists()
    assert result.output_grib.stat().st_size > 0
    assert result.manifest.exists()
    manifest = json.loads(result.manifest.read_text())
    assert manifest["recipe"]["name"] == "smoke"
    assert "extras" in manifest
    extras = manifest["extras"]
    assert "master_timeline" in extras
    assert len(extras["master_timeline"]) == len(result.master_timeline)
    assert extras["aligned_start"] == start_dt.isoformat()
    assert "cycles_used" in extras
