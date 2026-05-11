"""Tier resolution: turn a CategoryPlan + a time window into concrete download
plans (model, cycle, fhours), one per tier.

Tiers are sequential time slices. Tier 1 covers from `start` to
`start + tier1.until_hours`; tier 2 picks up where tier 1 left off; the last
tier (with `until_hours=None`) covers through `start + duration`.

Each tier picks its OWN latest ready cycle for its model — different models have
different cycle cadences and latencies, and forcing one shared cycle wastes
freshness. The combined GRIB will have heterogeneous reference times, which is
fine: each GRIB2 message is self-describing and routing software keys off
valid_time = reference + forecastTime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from grib_nomad.core.recipe import CategoryPlan
from grib_nomad.sources.base import ModelSpec, SourceError


@dataclass
class StepRule:
    from_hour: int
    to_hour: int
    step: int


@dataclass
class ResolvedTier:
    """One tier turned into something the runner can actually fetch."""

    model: ModelSpec
    cycle: datetime  # UTC, tz-aware
    fhours: list[int]  # forecast hours from `cycle`
    valid_times: list[datetime]  # cycle + fhour for each, tz-aware UTC


@dataclass
class ResolvedCategory:
    category: str
    tiers: list[ResolvedTier]
    coverage_end: datetime  # latest valid_time achieved (tz-aware UTC)


def native_hours(spec: ModelSpec) -> list[int]:
    """All forecast hours the model natively offers, sorted ascending."""
    hours: set[int] = set()
    for r in spec.steps:
        h = r.from_hour
        end = min(r.to_hour, spec.max_fhour)
        while h <= end:
            hours.add(h)
            h += r.step
    return sorted(h for h in hours if h <= spec.max_fhour)


def pick_cycle_for_window(
    spec: ModelSpec,
    *,
    window_start: datetime,
    now: datetime | None = None,
) -> datetime | None:
    """Latest ready cycle whose forecast window starts no later than `window_start`.

    "Ready" = `cycle + latency_hours <= now`.
    "Reaches `window_start`" = `cycle + max_fhour >= window_start` (i.e. forecast
    horizon is long enough to actually cover the start of the time slice).

    Returns None if nothing within 48 h satisfies both.
    """
    now = now or datetime.now(timezone.utc)
    cursor = now.replace(minute=0, second=0, microsecond=0)
    for _ in range(48):
        if cursor.hour in spec.cycles:
            ready = cursor + timedelta(hours=spec.latency_hours) <= now
            covers = cursor + timedelta(hours=spec.max_fhour) >= window_start
            if ready and covers:
                return cursor
        cursor -= timedelta(hours=1)
    return None


def resolve_category(
    plan: CategoryPlan,
    registry: dict[str, ModelSpec],
    *,
    aligned_start: datetime,
    aligned_end: datetime,
    default_step_hours: int,
    now: datetime | None = None,
    log_warning=lambda _msg: None,
) -> ResolvedCategory:
    """Walk `plan.tiers` in order, assigning each tier a sequential time slice.

    Each tier produces a `ResolvedTier` with concrete (cycle, fhours, valid_times).
    Forecast hours are picked so that:
      - valid_time falls within this tier's claimed time range,
      - valid_time >= `aligned_start` (no past data),
      - valid_time is reachable from the tier's chosen cycle on its native steps,
      - valid_time isn't already claimed by an earlier tier.

    No interpolation here — that's a downstream step driven by the master timeline.
    """
    now = now or datetime.now(timezone.utc)
    tier_resolved: list[ResolvedTier] = []
    cursor = aligned_start
    claimed_times: set[datetime] = set()

    for tier in plan.tiers:
        spec = registry[tier.model_id]

        # Compute the time slice this tier intends to cover
        if tier.until_hours is not None:
            tier_intent_end = min(
                aligned_start + timedelta(hours=tier.until_hours), aligned_end
            )
        else:
            tier_intent_end = aligned_end

        if tier_intent_end <= cursor:
            log_warning(
                f"  tier {spec.id} for {plan.category}: empty time slice — skipping"
            )
            continue

        # Pick the latest cycle of this model whose forecast horizon reaches the
        # start of the slice. If nothing reaches, skip the tier.
        cycle = pick_cycle_for_window(spec, window_start=cursor, now=now)
        if cycle is None:
            log_warning(
                f"  tier {spec.id} for {plan.category}: no ready cycle reaches "
                f"{cursor.isoformat()} — skipping"
            )
            continue

        # Model coverage may end before the tier's intended end
        model_max_dt = cycle + timedelta(hours=spec.max_fhour)
        slice_end = min(tier_intent_end, model_max_dt)

        # Native fhours within (cursor, slice_end] whose valid_time hasn't been claimed
        # We do not enforce a step grid here — `step_hours` is just an override
        # of the model's native cadence within this tier. The master timeline
        # downstream is the union of all chosen valid_times.
        fhours: list[int] = []
        valid_times: list[datetime] = []
        step_override = tier.step_hours

        for f in native_hours(spec):
            valid = cycle + timedelta(hours=f)
            # `<` (not `<=`) so the first tier's cursor=aligned_start is
            # inclusive — otherwise the first valid hour of the window
            # always gets dropped. Tier-to-tier dedup is handled by
            # `claimed_times` below, so this is safe.
            if valid < cursor:
                continue
            if valid > slice_end:
                continue
            if valid < aligned_start:
                continue
            if valid in claimed_times:
                continue
            if step_override is not None:
                # Restrict to step_override cadence relative to aligned_start
                offset_h = (valid - aligned_start).total_seconds() / 3600
                if abs(offset_h - round(offset_h)) > 1e-6:
                    continue
                if int(round(offset_h)) % step_override != 0:
                    continue
            fhours.append(f)
            valid_times.append(valid)

        if not fhours:
            log_warning(
                f"  tier {spec.id} for {plan.category}: native cadence yields "
                f"zero valid timesteps in [{cursor.isoformat()}, {slice_end.isoformat()}]"
            )
            continue

        for vt in valid_times:
            claimed_times.add(vt)
        tier_resolved.append(
            ResolvedTier(
                model=spec,
                cycle=cycle,
                fhours=fhours,
                valid_times=valid_times,
            )
        )
        cursor = max(cursor, slice_end)

    coverage_end = max(claimed_times) if claimed_times else aligned_start
    if coverage_end < aligned_end - timedelta(hours=1):
        gap_h = (aligned_end - coverage_end).total_seconds() / 3600
        log_warning(
            f"WARNING: category {plan.category!r} coverage ends at "
            f"{coverage_end.isoformat()} — {gap_h:.0f}h short of full duration"
        )

    return ResolvedCategory(
        category=plan.category,
        tiers=tier_resolved,
        coverage_end=coverage_end,
    )


def align_window(
    raw_start: datetime, duration_hours: int, *, snap_step_hours: int = 1
) -> tuple[datetime, datetime]:
    """Snap `raw_start` DOWN to the previous multiple of `snap_step_hours` from
    UTC midnight, return (aligned_start, aligned_start + duration_hours).
    Both tz-aware UTC.

    Floor (not ceil) so the window always *covers* the present moment. If the
    user runs at 15:29 local, they want data starting at 15:00 local — i.e.
    encompassing "now" — not at 16:00 which would skip the first half-hour.

    Default snap is 1 h. Pass a larger `snap_step_hours` for a coarser anchor.
    """
    if raw_start.tzinfo is None:
        raise ValueError("raw_start must be timezone-aware")
    raw_start = raw_start.astimezone(timezone.utc)
    midnight = raw_start.replace(hour=0, minute=0, second=0, microsecond=0)
    hours_since_midnight = (raw_start - midnight).total_seconds() / 3600
    import math

    aligned_h = math.floor(hours_since_midnight / snap_step_hours) * snap_step_hours
    aligned_start = midnight + timedelta(hours=aligned_h)
    aligned_end = aligned_start + timedelta(hours=duration_hours)
    return aligned_start, aligned_end
