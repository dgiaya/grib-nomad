"""Recipe runner — UI- and source-agnostic entry point used by CLI, TUI, and any
future GUI.

End-to-end flow:

    1. Resolve each category's tiers into per-tier (model, cycle, fhours,
       valid_times). Each tier picks its OWN latest ready cycle.
    2. Build the master timeline = union of every category's native valid_times.
    3. In parallel, fetch each tier's GRIB2 parts (NomadsSource / GomofsSource).
    4. Per category, time-interpolate native parts onto the master timeline.
       Categories whose native cadence equals the master cadence at every step
       are pass-through; sparser ones get bracketed-linear-interp into the gaps.
    5. Concatenate all categories' interpolated GRIB2 messages, sorted by
       (valid_time, category-display-priority, model_id, parameter_number) so
       routing software (e.g. qtVlm) sees wind first per timestep.
    6. Write the manifest sidecar with full per-message provenance.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from grib_nomad.core.cache import prune_stale
from grib_nomad.core.combine import (
    DownloadedPart,
    concatenate_gribs,
    write_manifest,
)
from grib_nomad.core.interpolate import interpolate_to_timeline
from grib_nomad.core.naming import build_filename, build_manifest_filename
from grib_nomad.core.recipe import Recipe
from grib_nomad.core.tier import (
    ResolvedCategory,
    ResolvedTier,
    align_window,
    resolve_category,
)
from grib_nomad.sources.base import (
    DownloadRequest,
    ModelSpec,
    Source,
    SourceError,
)


# Display-priority ordering used to sort messages within each valid_time so qtVlm
# defaults to wind instead of currents.
_CATEGORY_PRIORITY: dict[str, int] = {
    "wind": 0,
    "gust": 1,
    "pressure": 2,
    "temp_2m": 3,
    "wave": 4,
    "wind_wave": 5,
    "swell": 6,
    "sea_wind": 7,
    "current": 8,
    "precip_rate": 9,
    "cloud": 10,
}


@dataclass
class RunResult:
    output_grib: Path
    manifest: Path
    parts: list[DownloadedPart]
    init_cycle: datetime  # earliest cycle used (for backward compat)
    master_timeline: list[datetime]
    aligned_start: datetime
    aligned_end: datetime


Logger = Callable[[str], None]


def _noop(_: str) -> None:  # pragma: no cover
    pass


def run_recipe(
    recipe: Recipe,
    *,
    registry: dict[str, ModelSpec],
    sources: dict[str, Source],
    dest_dir: Path,
    log: Logger = _noop,
    quiet: bool = False,
    console: Console | None = None,
    now: datetime | None = None,
) -> RunResult:
    """Execute a recipe end-to-end. See module docstring for the flow."""
    if not recipe.categories:
        raise ValueError(f"recipe {recipe.name!r} has no categories")

    bbox = recipe.resolve_region()
    now = now or datetime.now(timezone.utc)
    raw_start = _resolve_recipe_start(recipe, now)
    aligned_start, aligned_end = align_window(raw_start, recipe.duration_hours)

    # Cache hygiene: drop any cycle directory whose forecast horizon is fully past.
    horizons = {
        ("nomads", spec.id): spec.max_fhour for spec in registry.values()
        if spec.source == "nomads"
    }
    horizons.update(
        {
            ("gomofs", spec.id): spec.max_fhour for spec in registry.values()
            if spec.source == "gomofs"
        }
    )
    pruned = prune_stale(now=now, max_horizon_hours=horizons)
    if pruned:
        log(f"cache: pruned {pruned} stale cycle dir(s)")
    log(
        f"window: {aligned_start.isoformat()} -> {aligned_end.isoformat()} "
        f"({recipe.duration_hours}h)"
    )

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. Resolve tiers per category — each picks its own freshest cycle
    resolved: list[ResolvedCategory] = []
    for plan in recipe.categories:
        rc = resolve_category(
            plan,
            registry,
            aligned_start=aligned_start,
            aligned_end=aligned_end,
            default_step_hours=1,  # not used as a hard grid; per-tier override only
            now=now,
            log_warning=log,
        )
        if not rc.tiers:
            log(
                f"WARNING: category {rc.category!r} resolved to nothing — skipping"
            )
            continue
        resolved.append(rc)
    if not resolved:
        raise SourceError("recipe resolved to zero downloads — nothing to fetch")

    # 2. Master timeline = union of every category's native valid_times.
    master_timeline = sorted(
        {vt for rc in resolved for tier in rc.tiers for vt in tier.valid_times}
    )
    log(f"master timeline: {len(master_timeline)} timesteps")

    # 3. Build flat (source, request) list for parallel download
    download_specs: list[tuple[Source, DownloadRequest, str]] = []
    for rc in resolved:
        for tier in rc.tiers:
            source = sources.get(tier.model.source)
            if source is None:
                raise SourceError(
                    f"no Source registered for {tier.model.source!r} (model {tier.model.id})"
                )
            req = DownloadRequest(
                model=tier.model,
                category=rc.category,
                bbox=bbox,
                cycle_date=tier.cycle.date(),
                cycle_hour=tier.cycle.hour,
                fhours=tier.fhours,
            )
            label = f"{rc.category}/{tier.model.id}/{tier.cycle.strftime('%H')}Z"
            download_specs.append((source, req, label))

    total_fhours = sum(len(req.fhours) for _, req, _ in download_specs)
    distinct = {src.name for src, _, _ in download_specs}
    log(
        f"plan: {len(download_specs)} request(s), {len(distinct)} source(s), "
        f"{total_fhours} forecast hour(s)"
    )

    # Per-category fhour totals so each category gets its own progress row.
    cat_totals: dict[str, int] = defaultdict(int)
    for _, req, _ in download_specs:
        cat_totals[req.category] += len(req.fhours)

    # 4. Download in parallel + 5. interpolate per category + 6. concat
    with tempfile.TemporaryDirectory(prefix="grib_nomad_", dir=str(dest_dir)) as tmp:
        tmp_dir = Path(tmp)
        progress_console = console or Console()
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(compact=True),
            console=progress_console,
            transient=False,
            disable=quiet,
        )
        download_task = progress.add_task(
            "[bold]downloading", total=total_fhours
        )
        cat_tasks = {
            cat: progress.add_task(
                f"[cyan]  {cat}  [dim]queued", total=count
            )
            for cat, count in cat_totals.items()
        }

        # Throughput tracker: total bytes seen so far, started when first part lands.
        progress_lock = threading.Lock()
        throughput = {"bytes": 0, "started_at": None, "last_file": None}

        def _speed_str() -> str:
            started = throughput["started_at"]
            if started is None:
                return ""
            elapsed = max(time.monotonic() - started, 0.001)
            n_bytes = throughput["bytes"]
            # Pick units adaptively so very-small NOMADS payloads don't
            # render as "0.0 MB · 0.01 MB/s" when actual transfer is in KB.
            if n_bytes < 1_000_000:
                size = f"{n_bytes / 1e3:.0f} KB"
            else:
                size = f"{n_bytes / 1e6:.1f} MB"
            rate = n_bytes / elapsed
            if rate < 1_000_000:
                rate_s = f"{rate / 1e3:.0f} KB/s"
            else:
                rate_s = f"{rate / 1e6:.2f} MB/s"
            return f" {size} · {rate_s}"

        def on_part(part: DownloadedPart) -> None:
            with progress_lock:
                if throughput["started_at"] is None:
                    throughput["started_at"] = time.monotonic()
                throughput["bytes"] += max(0, part.bytes_)
                throughput["last_file"] = (
                    f"{part.model_id} f{part.fhour:03d}"
                )
                speed = _speed_str()
            progress.update(
                download_task,
                advance=1,
                description=f"[bold]downloading{speed}",
            )
            if part.category in cat_tasks:
                progress.update(
                    cat_tasks[part.category],
                    advance=1,
                    description=(
                        f"[cyan]  {part.category}  "
                        f"[dim]{part.model_id} f{part.fhour:03d}"
                    ),
                )

        def make_on_status(category: str):
            """Per-category status callback so a Source can surface what its
            long-running setup phases are doing — visible in the bar before
            any forecast hour completes."""

            def _set(msg: str) -> None:
                if category in cat_tasks:
                    progress.update(
                        cat_tasks[category],
                        description=f"[cyan]  {category}  [dim]{msg}",
                    )

            return _set

        all_native_parts: list[DownloadedPart] = []
        parts_lock = threading.Lock()

        with progress, ThreadPoolExecutor(
            max_workers=max(1, len(download_specs)),
            thread_name_prefix="grib_nomad",
        ) as ex:
            futures = {
                ex.submit(
                    _run_one_request,
                    src,
                    req,
                    tmp_dir,
                    on_part,
                    make_on_status(req.category),
                ): label
                for src, req, label in download_specs
            }
            try:
                for f in as_completed(futures):
                    parts = f.result()
                    with parts_lock:
                        all_native_parts.extend(parts)
            except Exception:
                for fut in futures:
                    fut.cancel()
                raise

        # 5. Interpolate each category onto the master timeline
        interp_task = progress.add_task(
            "interpolating", total=len(resolved), disable=quiet
        ) if not quiet else None
        interpolated_parts: list[DownloadedPart] = []
        for rc in resolved:
            cat_parts = [p for p in all_native_parts if p.category == rc.category]
            if not cat_parts:
                continue
            interp_path = tmp_dir / f"interp_{rc.category}.grb2"
            combined, achieved = interpolate_to_timeline(
                cat_parts, master_timeline, interp_path, log_warning=log
            )
            log(
                f"  {rc.category}: interpolated {len(cat_parts)} native parts -> "
                f"{len(achieved)} master timesteps"
            )
            interpolated_parts.append(combined)
            if interp_task is not None:
                progress.update(interp_task, advance=1)

        # Sort by display priority so qtVlm shows wind first per category
        interpolated_parts.sort(
            key=lambda p: _CATEGORY_PRIORITY.get(p.category, 99)
        )

        out_name = build_filename(recipe, aligned_start)
        manifest_name = build_manifest_filename(out_name)
        out_path = dest_dir / out_name
        manifest_path = dest_dir / manifest_name

        log(f"combining {len(interpolated_parts)} category file(s) -> {out_path.name}")
        concatenate_gribs(interpolated_parts, out_path)

        # Manifest: report both NATIVE parts (provenance) and the master timeline
        write_manifest(
            recipe=recipe,
            init=aligned_start,
            parts=all_native_parts,
            dest=manifest_path,
            output_grib=out_path,
            extras={
                "master_timeline": [t.isoformat() for t in master_timeline],
                "aligned_start": aligned_start.isoformat(),
                "aligned_end": aligned_end.isoformat(),
                "duration_hours": recipe.duration_hours,
                "per_category_coverage_end": {
                    rc.category: rc.coverage_end.isoformat() for rc in resolved
                },
                "cycles_used": sorted(
                    {tier.cycle.isoformat() for rc in resolved for tier in rc.tiers}
                ),
            },
        )

    earliest_cycle = min(
        tier.cycle for rc in resolved for tier in rc.tiers
    )
    return RunResult(
        output_grib=out_path,
        manifest=manifest_path,
        parts=all_native_parts,
        init_cycle=earliest_cycle,
        master_timeline=master_timeline,
        aligned_start=aligned_start,
        aligned_end=aligned_end,
    )


def _run_one_request(
    source: Source,
    request: DownloadRequest,
    tmp_dir: Path,
    on_part: Callable[[DownloadedPart], None],
    on_status: Callable[[str], None],
) -> list[DownloadedPart]:
    return source.download(
        request, tmp_dir, on_part=on_part, on_status=on_status
    )


def _resolve_recipe_start(recipe: Recipe, now: datetime) -> datetime:
    """Recipe.start may be None (= now), naive (= local TZ), or tz-aware."""
    if recipe.start is None:
        return now
    if recipe.start.tzinfo is None:
        # Treat naive as local time; convert to UTC.
        local_tz = datetime.now().astimezone().tzinfo
        return recipe.start.replace(tzinfo=local_tz).astimezone(timezone.utc)
    return recipe.start.astimezone(timezone.utc)
