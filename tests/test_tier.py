from datetime import datetime, timedelta, timezone

from grib_nomad.core.recipe import CategoryPlan, ModelTier
from grib_nomad.core.tier import (
    align_window,
    native_hours,
    pick_cycle_for_window,
    resolve_category,
)
from grib_nomad.models import load_registry


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_native_hours_uses_step_rules_and_caps_at_max():
    reg = load_registry()
    gfs = reg["gfs_0p25"]
    hours = native_hours(gfs)
    # GFS is hourly to 120, then 3-hourly through 384
    assert hours[0] == 0 and hours[120] == 120
    assert 123 in hours and 126 in hours and 384 in hours
    assert hours[-1] == 384


def test_align_window_snaps_down_to_integer_hour():
    """We floor `raw_start` so the window covers 'now' instead of skipping past it."""
    raw = _utc(2026, 5, 8, 17, 30)
    start, end = align_window(raw, duration_hours=24)
    assert start == _utc(2026, 5, 8, 17)
    assert end == _utc(2026, 5, 9, 17)


def test_align_window_exact_hour_is_idempotent():
    raw = _utc(2026, 5, 8, 17, 0)
    start, _ = align_window(raw, duration_hours=24)
    assert start == _utc(2026, 5, 8, 17)


def test_pick_cycle_picks_latest_ready_within_horizon():
    reg = load_registry()
    hrrr = reg["hrrr_conus_hourly"]
    # Now: 2026-05-08 18:00Z. HRRR-hourly runs every hour, latency 2h.
    # Latest ready cycle = 16Z (16Z + 2h = 18Z is exactly "now"-ready).
    now = _utc(2026, 5, 8, 18, 0)
    window_start = _utc(2026, 5, 8, 18, 0)
    cycle = pick_cycle_for_window(hrrr, window_start=window_start, now=now)
    assert cycle == _utc(2026, 5, 8, 16)
    # And cycle must reach window_start (16Z + 18h = past 18Z, fine)
    assert (cycle + timedelta(hours=hrrr.max_fhour)) >= window_start


def test_pick_cycle_extended_hrrr_skips_non_extended_hours():
    """`hrrr_conus_sfc` is pinned to 00/06/12/18 cycles only — the hourly
    cycles in between (which only run to f18) shouldn't be picked even when
    they're more recent."""
    reg = load_registry()
    hrrr_ext = reg["hrrr_conus_sfc"]
    now = _utc(2026, 5, 8, 18, 30)
    window_start = _utc(2026, 5, 8, 18, 30)
    cycle = pick_cycle_for_window(hrrr_ext, window_start=window_start, now=now)
    # Latency=3h, latest extended ready at 18:30Z is 12Z (12+3=15 ≤ 18:30);
    # 18Z would only be ready at 21Z.
    assert cycle == _utc(2026, 5, 8, 12)
    assert cycle.hour in {0, 6, 12, 18}


def test_pick_cycle_returns_none_if_horizon_too_short():
    reg = load_registry()
    hrrr = reg["hrrr_conus_sfc"]  # max_fhour=48
    now = _utc(2026, 5, 8, 18, 0)
    # Ask for a window starting 5 days in the future — HRRR can't reach it
    far_window = _utc(2026, 5, 13, 18, 0)
    cycle = pick_cycle_for_window(hrrr, window_start=far_window, now=now)
    assert cycle is None


def test_resolve_category_two_tier_no_overlap():
    """HRRR for first 48h, then GFS for the remainder. Each picks its own cycle."""
    reg = load_registry()
    plan = CategoryPlan(
        category="wind",
        tiers=[
            ModelTier(model_id="hrrr_conus_sfc", until_hours=48),
            ModelTier(model_id="gfs_0p25"),  # rest of duration
        ],
    )
    aligned_start = _utc(2026, 5, 8, 18)
    aligned_end = aligned_start + timedelta(hours=168)
    now = _utc(2026, 5, 8, 18, 30)
    rc = resolve_category(
        plan,
        reg,
        aligned_start=aligned_start,
        aligned_end=aligned_end,
        default_step_hours=1,
        now=now,
    )
    assert len(rc.tiers) == 2
    hrrr_tier, gfs_tier = rc.tiers

    assert hrrr_tier.model.id == "hrrr_conus_sfc"
    # Every HRRR valid_time should fall in [aligned_start, aligned_start+48h]
    # (aligned_start is inclusive — the first hour of the window is in scope).
    for vt in hrrr_tier.valid_times:
        assert aligned_start <= vt <= aligned_start + timedelta(hours=48)

    assert gfs_tier.model.id == "gfs_0p25"
    # GFS picks up strictly after HRRR's last claimed time
    last_hrrr = max(hrrr_tier.valid_times)
    assert min(gfs_tier.valid_times) > last_hrrr
    # GFS extends through the rest of the duration (capped at GFS max horizon)
    assert max(gfs_tier.valid_times) <= aligned_end


def test_resolve_category_warns_on_short_coverage():
    """GoMOFS only covers 72h; recipe wants 168h. Should warn but still succeed."""
    reg = load_registry()
    plan = CategoryPlan(
        category="current",
        tiers=[ModelTier(model_id="gomofs_currents")],
    )
    aligned_start = _utc(2026, 5, 8, 18)
    aligned_end = aligned_start + timedelta(hours=168)
    now = _utc(2026, 5, 8, 18, 30)
    warnings: list[str] = []
    rc = resolve_category(
        plan,
        reg,
        aligned_start=aligned_start,
        aligned_end=aligned_end,
        default_step_hours=3,
        now=now,
        log_warning=warnings.append,
    )
    assert len(rc.tiers) == 1
    # Coverage should end around 72h, not 168h
    assert rc.coverage_end <= aligned_start + timedelta(hours=72)
    assert any("short of full duration" in w for w in warnings)


def test_resolve_category_no_pre_start_timesteps():
    """No valid_time may fall before aligned_start, even if the cycle does."""
    reg = load_registry()
    plan = CategoryPlan(
        category="wind",
        tiers=[ModelTier(model_id="hrrr_conus_sfc")],
    )
    aligned_start = _utc(2026, 5, 8, 18)
    aligned_end = aligned_start + timedelta(hours=24)
    # cycle picked will be before aligned_start; that's fine, but valid_times must not be
    now = _utc(2026, 5, 8, 18, 30)
    rc = resolve_category(
        plan, reg,
        aligned_start=aligned_start, aligned_end=aligned_end,
        default_step_hours=1, now=now,
    )
    for tier in rc.tiers:
        for vt in tier.valid_times:
            assert vt >= aligned_start
