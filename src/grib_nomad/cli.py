"""CLI frontend.

Usage examples:

    grib-nomad regions list
    grib-nomad models list
    grib-nomad models show gfs_0p25

    # 7-day download starting now: HRRR for first 48h, then GFS; GFS Wave full duration;
    # GoMOFS currents (caps at GoMOFS' 72h horizon, with a coverage warning).
    grib-nomad download \\
        --region gulf-of-maine-wide \\
        --duration 7d \\
        --tier wind:hrrr_conus_sfc:48 \\
        --tier wind:gfs_0p25 \\
        --tier wave:gfs_wave_global_0p25 \\
        --tier current:gomofs_currents

    grib-nomad recipe save my-bermuda --from-last
    grib-nomad recipe run my-bermuda
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from grib_nomad import __version__
from grib_nomad.core.config import load_yaml, recipes_path, save_yaml
from grib_nomad.core.recipe import CategoryPlan, ModelTier, Recipe
from grib_nomad.core.regions import REGIONS, parse_bbox_string
from grib_nomad.core.runner import run_recipe
from grib_nomad.models import load_registry, models_for_category
from grib_nomad.sources.ecmwf_open import EcmwfOpenSource
from grib_nomad.sources.gomofs import GomofsSource
from grib_nomad.sources.nomads import NomadsSource
from grib_nomad.sources.nwps import NwpsSource

console = Console()


def _build_sources(workers: int) -> dict:
    # NOMADS and GoMOFS have very different concurrency budgets:
    #   - NOMADS sits behind Akamai with a documented ~120 hits/min cap, and
    #     NomadsSource enforces this internally with a token bucket + a
    #     Semaphore(4) fan-out gate. Raising `workers` past ~8 doesn't help
    #     NOMADS throughput (the bucket dominates) but does risk burst-tripping.
    #   - GoMOFS hits S3, which has effectively no per-IP cap for our volume.
    #     16 concurrent Range requests is conservative and still saturates a
    #     gigabit link. We don't want a low `--workers` (set for NOMADS
    #     politeness) to needlessly slow the S3 path.
    # So we floor GoMOFS at 16 and let users scale further via --workers.
    return {
        "nomads": NomadsSource(max_workers=workers),
        "ecmwf_open": EcmwfOpenSource(),
        "gomofs": GomofsSource(max_workers=max(16, workers)),
        # NWPS is one bundle download per (cycle, category, bbox), not
        # per-fhour, so it doesn't need its own worker pool — the rate
        # limiting reuses NomadsSource's shared token bucket.
        "nwps": NwpsSource(),
    }


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="grib-nomad")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Friendly NOMADS GRIB downloader for weather routing."""
    if ctx.invoked_subcommand is None:
        from grib_nomad.tui import run_tui

        run_tui()


# --- regions ----------------------------------------------------------------

@main.group()
def regions() -> None:
    """Built-in region presets."""


@regions.command("list")
def regions_list() -> None:
    table = Table(title="Built-in regions")
    table.add_column("name", style="cyan")
    table.add_column("lat range")
    table.add_column("lon range")
    for name, bbox in REGIONS.items():
        table.add_row(
            name,
            f"{bbox.lat_min:+.1f} .. {bbox.lat_max:+.1f}",
            f"{bbox.lon_min:+.1f} .. {bbox.lon_max:+.1f}",
        )
    console.print(table)


# --- models -----------------------------------------------------------------

@main.group()
def models() -> None:
    """Inspect the bundled model registry."""


@models.command("list")
@click.option("--category", help="Filter to models that supply this category.")
def models_list(category: str | None) -> None:
    registry = load_registry()
    rows = registry.values()
    if category:
        rows = models_for_category(registry, category)
    table = Table(title=f"Models{f' (category={category})' if category else ''}")
    table.add_column("id", style="cyan")
    table.add_column("display")
    table.add_column("source")
    table.add_column("cycles")
    table.add_column("max f")
    table.add_column("categories")
    for m in sorted(rows, key=lambda x: x.id):
        cycles = ",".join(f"{c:02d}" for c in m.cycles[:6]) + (
            f" (+{len(m.cycles) - 6})" if len(m.cycles) > 6 else ""
        )
        table.add_row(
            m.id,
            m.display_name,
            m.source,
            cycles,
            str(m.max_fhour),
            ",".join(sorted(m.categories)),
        )
    console.print(table)


@models.command("show")
@click.argument("model_id")
def models_show(model_id: str) -> None:
    registry = load_registry()
    if model_id not in registry:
        raise click.ClickException(
            f"unknown model {model_id!r}; valid ids: {', '.join(sorted(registry))}"
        )
    m = registry[model_id]
    console.print(f"[bold cyan]{m.id}[/]  {m.display_name}")
    console.print(f"source: {m.source}")
    console.print(f"cycles: {sorted(m.cycles)}")
    console.print(f"max forecast hour: {m.max_fhour}")
    console.print(f"latency: ~{m.latency_hours}h")
    console.print("step rules:")
    for s in m.steps:
        console.print(f"  {s.from_hour:>3}..{s.to_hour:<3} every {s.step}h")
    console.print("categories:")
    for cname, cv in m.categories.items():
        console.print(f"  {cname}: vars={list(cv.vars)} levels={list(cv.levels)}")


# --- download ---------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([dhDH]?)\s*$")


def _parse_duration(s: str) -> int:
    """Parse '7d', '168h', '12h', '3.5d', '24' to integer hours."""
    m = _DURATION_RE.match(s)
    if not m:
        raise click.UsageError(
            f"--duration must be like '7d' / '168h' / '24', got {s!r}"
        )
    value, unit = m.group(1), (m.group(2) or "h").lower()
    hours = float(value) * (24 if unit == "d" else 1)
    if hours < 1 or hours > 24 * 30:
        raise click.UsageError(
            f"--duration must be between 1h and 30d, got {hours:g}h"
        )
    return int(round(hours))


def _parse_start(s: str | None) -> datetime | None:
    """Parse '--start' value. None or 'now' -> None (= runtime now)."""
    if not s or s.strip().lower() == "now":
        return None
    try:
        d = datetime.fromisoformat(s.strip())
    except ValueError as e:
        raise click.UsageError(
            f"--start must be ISO format (e.g. '2026-05-08 14:00' or "
            f"'2026-05-08T14:00-04:00'), got {s!r}"
        ) from e
    if d.tzinfo is None:
        # Naive: assume local TZ
        d = d.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return d.astimezone(timezone.utc)


def _parse_tier_spec(spec: str) -> tuple[str, ModelTier]:
    """Parse 'category:model_id[:until_hours[:step_hours]]'.

    Examples:
        wind:hrrr_conus_sfc:48          -> first tier, covers up to start+48h
        wind:gfs_0p25                   -> covers rest of duration
        wave:gfs_wave_global_0p25:96:6  -> until +96h, step 6h within
    """
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 4:
        raise click.UsageError(
            f"--tier must be 'category:model_id[:until_hours[:step_hours]]', "
            f"got {spec!r}"
        )
    category = parts[0]
    model_id = parts[1]
    until_hours = int(parts[2]) if len(parts) >= 3 and parts[2] else None
    step_hours = int(parts[3]) if len(parts) == 4 and parts[3] else None
    return category, ModelTier(
        model_id=model_id, until_hours=until_hours, step_hours=step_hours
    )


@main.command()
@click.option("--region", "region_name", help="Built-in region name (see `regions list`).")
@click.option(
    "--bbox",
    "bbox_str",
    help="Custom bounding box: 'lat_min,lat_max,lon_min,lon_max'.",
)
@click.option(
    "--tier",
    "tier_specs",
    multiple=True,
    required=True,
    help=(
        "Add a tier: 'category:model_id[:until_hours[:step_hours]]'. "
        "Repeat for multi-tier and multi-category. The first tier of each "
        "category starts at --start; subsequent tiers pick up from the previous "
        "tier's end. A tier with no `until_hours` covers the rest of `--duration`."
    ),
)
@click.option(
    "--duration",
    default="7d",
    show_default=True,
    help="How long to cover. '7d', '168h', '24', etc.",
)
@click.option(
    "--start",
    default=None,
    help=(
        "Window start, ISO datetime. If timezone is omitted it's interpreted "
        "as local TZ. Default: now."
    ),
)
@click.option(
    "--out-dir",
    default="downloads",
    show_default=True,
)
@click.option("--name", default="adhoc", help="Recipe name (recorded in the manifest).")
@click.option("--save-as", help="Also save this run as a named recipe.")
@click.option(
    "--workers",
    "-j",
    default=8,
    show_default=True,
    type=click.IntRange(min=1, max=64),
    help="Concurrent fetches for NOMADS (token-bucket-throttled internally regardless). GoMOFS has its own higher floor (S3 doesn't need throttling). 8 is a sensible default.",
)
def download(
    region_name: str | None,
    bbox_str: str | None,
    tier_specs: tuple[str, ...],
    duration: str,
    start: str | None,
    out_dir: str,
    name: str,
    save_as: str | None,
    workers: int,
) -> None:
    """Run a one-shot recipe from CLI flags."""
    if not (region_name or bbox_str):
        raise click.UsageError("specify --region or --bbox")
    if region_name and bbox_str:
        raise click.UsageError("--region and --bbox are mutually exclusive")

    region = region_name if region_name else parse_bbox_string(bbox_str)  # type: ignore[arg-type]
    duration_hours = _parse_duration(duration)
    start_dt = _parse_start(start)

    by_category: dict[str, list[ModelTier]] = defaultdict(list)
    for spec in tier_specs:
        category, tier = _parse_tier_spec(spec)
        by_category[category].append(tier)

    plans = [
        CategoryPlan(category=cat, tiers=tiers)
        for cat, tiers in by_category.items()
    ]
    recipe = Recipe(
        name=name,
        region=region,
        duration_hours=duration_hours,
        start=start_dt,
        categories=plans,
    )

    if save_as:
        _save_recipe(recipe, name=save_as)
        console.print(f"[green]saved recipe[/] {save_as}")

    _execute(recipe, Path(out_dir), workers=workers)


@main.group()
def recipe() -> None:
    """Save and re-run named recipes."""


@recipe.command("list")
def recipe_list() -> None:
    data = load_yaml(recipes_path())
    if not data:
        console.print("[yellow]no recipes saved yet[/]")
        return
    table = Table(title=f"Recipes ({recipes_path()})")
    table.add_column("name", style="cyan")
    table.add_column("region")
    table.add_column("duration")
    table.add_column("start")
    table.add_column("categories")
    for name, payload in data.items():
        r = Recipe.from_dict(payload)
        cats = ", ".join(
            f"{c.category}({len(c.tiers)})" for c in r.categories
        )
        region = r.region if isinstance(r.region, str) else "<custom bbox>"
        start_label = r.start.isoformat() if r.start else "now"
        table.add_row(name, region, f"{r.duration_hours}h", start_label, cats)
    console.print(table)


@recipe.command("show")
@click.argument("name")
def recipe_show(name: str) -> None:
    data = load_yaml(recipes_path())
    if name not in data:
        raise click.ClickException(f"no recipe named {name!r}")
    console.print(yaml.safe_dump(data[name], sort_keys=False))


@recipe.command("delete")
@click.argument("name")
def recipe_delete(name: str) -> None:
    data = load_yaml(recipes_path())
    if name not in data:
        raise click.ClickException(f"no recipe named {name!r}")
    del data[name]
    save_yaml(recipes_path(), data)
    console.print(f"[green]deleted[/] {name}")


@recipe.command("run")
@click.argument("name")
@click.option("--out-dir", default="downloads", show_default=True)
@click.option(
    "--start",
    help="Override the recipe's start time (ISO datetime; default: recipe's saved start, or now).",
)
@click.option(
    "--duration",
    help="Override the recipe's duration (e.g. '5d').",
)
@click.option(
    "--workers",
    "-j",
    default=8,
    show_default=True,
    type=click.IntRange(min=1, max=64),
)
def recipe_run(
    name: str,
    out_dir: str,
    start: str | None,
    duration: str | None,
    workers: int,
) -> None:
    data = load_yaml(recipes_path())
    if name not in data:
        raise click.ClickException(f"no recipe named {name!r}")
    r = Recipe.from_dict(data[name])
    if start is not None:
        r.start = _parse_start(start)
    if duration is not None:
        r.duration_hours = _parse_duration(duration)
    _execute(r, Path(out_dir), workers=workers)


# --- helpers ----------------------------------------------------------------

def _save_recipe(recipe: Recipe, *, name: str) -> None:
    data = load_yaml(recipes_path())
    saved = recipe.to_dict()
    saved["name"] = name
    data[name] = saved
    save_yaml(recipes_path(), data)


def _execute(recipe: Recipe, out_dir: Path, *, workers: int = 6) -> None:
    registry = load_registry()
    sources = _build_sources(workers)
    try:
        result = run_recipe(
            recipe,
            registry=registry,
            sources=sources,
            dest_dir=out_dir,
            log=lambda m: console.print(f"[dim]{m}[/]"),
            console=console,
        )
    except Exception as e:
        console.print(f"[red]error:[/] {e}")
        sys.exit(1)
    console.print(f"[green]wrote[/] {result.output_grib}")
    console.print(f"[green]wrote[/] {result.manifest}")
    console.print(
        f"[dim]master timeline: {len(result.master_timeline)} timesteps "
        f"from {result.aligned_start.isoformat()} to {result.aligned_end.isoformat()}[/]"
    )


# --- inspect ----------------------------------------------------------------

@main.command()
@click.argument("grib_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--full",
    is_flag=True,
    help="List every message individually instead of grouping by parameter.",
)
def inspect(grib_file: str, full: bool) -> None:
    """Inspect a GRIB2 file's parameter codes and routing-software compatibility.

    Reads each message's (discipline, parameterCategory, parameterNumber)
    triple — the codes routing apps actually look up — and cross-references
    against a built-in table of variables OpenCPN/qtVlm/XyGrib recognize.
    Useful to verify a recipe's output is going to render correctly before
    you load it into your routing software.
    """
    try:
        from eccodes import (
            codes_get,
            codes_get_long,
            codes_grib_new_from_file,
            codes_release,
        )
    except ImportError as e:  # pragma: no cover
        raise click.ClickException(
            f"eccodes is required for `inspect` (install [gomofs] extra): {e}"
        ) from e

    from grib_nomad.core.routing_compat import lookup

    # Roll up messages by (disc, cat, num, level) so 72 fhours of UGRD render
    # as one row instead of 72 rows.
    rollup: dict[tuple, dict] = {}
    individual_rows: list[dict] = []

    with open(grib_file, "rb") as f:
        msg_idx = 0
        while True:
            gid = codes_grib_new_from_file(f)
            if gid is None:
                break
            msg_idx += 1
            try:
                disc = int(codes_get_long(gid, "discipline"))
                cat = int(codes_get_long(gid, "parameterCategory"))
                num = int(codes_get_long(gid, "parameterNumber"))
                try:
                    type_lev = int(codes_get_long(gid, "typeOfFirstFixedSurface"))
                    scale = int(codes_get_long(gid, "scaleFactorOfFirstFixedSurface"))
                    scaled = int(codes_get_long(gid, "scaledValueOfFirstFixedSurface"))
                except Exception:
                    type_lev = scale = scaled = -1
                fhour = int(codes_get_long(gid, "forecastTime"))
                ni = int(codes_get_long(gid, "Ni"))
                nj = int(codes_get_long(gid, "Nj"))
                eccodes_short = codes_get(gid, "shortName")
            finally:
                codes_release(gid)

            spec = lookup(disc, cat, num)
            key = (disc, cat, num, type_lev, scale, scaled)
            if key not in rollup:
                rollup[key] = {
                    "discipline": disc,
                    "category": cat,
                    "param": num,
                    "type_lev": type_lev,
                    "level_value": _format_level(type_lev, scale, scaled),
                    "spec": spec,
                    "eccodes_short": eccodes_short,
                    "fhours": [],
                    "ni_nj": (ni, nj),
                }
            rollup[key]["fhours"].append(fhour)

            if full:
                individual_rows.append(
                    {
                        "msg": msg_idx,
                        "disc": disc,
                        "cat": cat,
                        "param": num,
                        "fhour": fhour,
                        "ni_nj": (ni, nj),
                        "spec": spec,
                        "eccodes_short": eccodes_short,
                    }
                )

    if not rollup:
        console.print(f"[yellow]no GRIB2 messages found in {grib_file}[/]")
        return

    console.print(
        f"[bold]{grib_file}[/] — {sum(len(r['fhours']) for r in rollup.values())} "
        f"messages, {len(rollup)} unique (param, level) combinations"
    )

    table = Table(title="Routing-software compatibility")
    table.add_column("disc/cat/num", style="dim")
    table.add_column("eccodes")
    table.add_column("standard short", style="cyan")
    table.add_column("description")
    table.add_column("level")
    table.add_column("# msgs", justify="right")
    table.add_column("recognized by")
    table.add_column("status")

    for key in sorted(rollup):
        row = rollup[key]
        spec = row["spec"]
        if spec is None:
            standard_short = "—"
            desc = "(no standard match)"
            recognized = "?"
            status = "[yellow]?[/]"
        else:
            standard_short = spec.short
            desc = spec.long_name
            recognized = ", ".join(spec.recognized_by)
            status = "[green]✓[/]"
        table.add_row(
            f"{row['discipline']:>2}/{row['category']:>2}/{row['param']:>2}",
            row["eccodes_short"],
            standard_short,
            desc,
            row["level_value"],
            str(len(row["fhours"])),
            recognized,
            status,
        )
    console.print(table)

    # Per-category summary aimed at the user's question: will my routing app
    # see wind, wave, current, precip in this file?
    console.print()
    by_app: dict[str, set[str]] = {
        "OpenCPN": set(),
        "qtVlm": set(),
        "XyGrib": set(),
    }
    unknown_count = 0
    for row in rollup.values():
        spec = row["spec"]
        if spec is None:
            unknown_count += 1
            continue
        for app in by_app:
            if app in spec.recognized_by:
                by_app[app].add(spec.short)
    summary = Table(title="What each routing app should display")
    summary.add_column("app", style="cyan")
    summary.add_column("# variables it recognizes", justify="right")
    summary.add_column("variables")
    for app, names in by_app.items():
        summary.add_row(
            app,
            str(len(names)),
            ", ".join(sorted(names)) if names else "(none)",
        )
    console.print(summary)
    if unknown_count:
        console.print(
            f"[yellow]{unknown_count} param-code combination(s) not in "
            f"`routing_compat.ROUTING_PARAMS` — they may still render in "
            f"some apps, but cross-check the WMO tables.[/]"
        )

    if full:
        console.print()
        console.print("[bold]Per-message detail:[/]")
        for r in individual_rows:
            spec = r["spec"]
            mark = "✓" if spec else "?"
            short = spec.short if spec else "—"
            console.print(
                f"  msg {r['msg']:>3}  "
                f"({r['disc']:>2}/{r['cat']:>2}/{r['param']:>2})  "
                f"{short:<6}  f{r['fhour']:03d}  "
                f"{r['ni_nj'][0]}×{r['ni_nj'][1]}  {mark}"
            )


_FIXED_SURFACE_NAMES = {
    1: "ground/water surface",
    103: "specified height above ground",
    101: "mean sea level",
    104: "specified altitude above MSL",
    105: "specified altitude (model)",
    100: "isobaric surface",
    102: "specified altitude above MSL",
    106: "below land surface",
    160: "below sea level",
    8: "nominal top of atmosphere",
    10: "entire atmosphere (single layer)",
}


def _format_level(type_lev: int, scale: int, scaled: int) -> str:
    name = _FIXED_SURFACE_NAMES.get(type_lev, f"type {type_lev}")
    if scaled <= 0 and scale == 0:
        return name
    try:
        value = scaled * (10 ** -scale)
    except Exception:
        value = scaled
    return f"{name} = {value:g}"


if __name__ == "__main__":
    main()
