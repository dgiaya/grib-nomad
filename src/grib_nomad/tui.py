"""Interactive TUI menu — minimal v0.

Walks the user through region → categories → per-category tiers → confirm → download.
For category model selection, the curated registry is filtered to models that supply
the chosen category; "Show all" is a stretch goal once `models refresh` is wired up.
"""

from __future__ import annotations

from pathlib import Path

import questionary
from rich.console import Console

from grib_nomad.core.recipe import CategoryPlan, ModelTier, Recipe
from grib_nomad.core.regions import REGIONS
from grib_nomad.core.runner import run_recipe
from grib_nomad.models import load_registry, models_for_category
from grib_nomad.cli import _parse_duration
from grib_nomad.sources.ecmwf_open import EcmwfOpenSource
from grib_nomad.sources.gomofs import GomofsSource
from grib_nomad.sources.nwps import NwpsSource
from grib_nomad.sources.nomads import NomadsSource

console = Console()

# Keep the top-level UX simple: pick from the categories we've seeded the registry with.
DEFAULT_CATEGORIES = [
    "wind",
    "gust",
    "wave",
    "current",
    "pressure",
    "temp_2m",
    "precip_rate",
]


def run_tui() -> None:
    console.print("[bold cyan]grib_nomad[/] — interactive setup")

    region_name = questionary.select(
        "Region:",
        choices=sorted(REGIONS.keys()) + ["custom bounding box"],
    ).ask()
    if region_name is None:
        return
    if region_name == "custom bounding box":
        bbox_str = questionary.text(
            "Enter 'lat_min,lat_max,lon_min,lon_max':"
        ).ask()
        if not bbox_str:
            return
        from grib_nomad.core.regions import parse_bbox_string

        region = parse_bbox_string(bbox_str)
    else:
        region = region_name

    chosen_cats = questionary.checkbox(
        "Pick categories to include:", choices=DEFAULT_CATEGORIES
    ).ask()
    if not chosen_cats:
        console.print("[yellow]no categories selected; aborting[/]")
        return

    registry = load_registry()
    plans: list[CategoryPlan] = []
    for cat in chosen_cats:
        candidates = models_for_category(registry, cat)
        if not candidates:
            console.print(f"[yellow]no seeded models for category {cat!r} — skipping[/]")
            continue

        choice_labels = [
            f"{m.id}  ({m.display_name}, max f{m.max_fhour})" for m in candidates
        ]
        labels_to_id = {label: m.id for label, m in zip(choice_labels, candidates, strict=False)}

        first = questionary.select(
            f"[{cat}] primary model:", choices=choice_labels
        ).ask()
        if first is None:
            return
        primary_id = labels_to_id[first]
        primary_max = registry[primary_id].max_fhour

        primary_until = int(
            questionary.text(
                f"[{cat}] primary covers from start to +N hours (max {primary_max}):",
                default=str(min(48, primary_max)),
            ).ask()
            or "0"
        )

        tiers = [ModelTier(model_id=primary_id, until_hours=primary_until)]

        if questionary.confirm(
            f"[{cat}] add a second-tier (longer-range) model?",
            default=False,
        ).ask():
            second = questionary.select(
                f"[{cat}] second-tier model:",
                choices=[label for label in choice_labels if labels_to_id[label] != primary_id],
            ).ask()
            if second is not None:
                second_id = labels_to_id[second]
                tiers.append(ModelTier(model_id=second_id))  # covers rest

        plans.append(CategoryPlan(category=cat, tiers=tiers))

    if not plans:
        return

    duration_str = questionary.text(
        "Duration ('7d', '168h', etc):", default="7d"
    ).ask() or "7d"
    duration_hours = _parse_duration(duration_str)
    recipe_name = questionary.text("Recipe name (recorded in manifest):", default="adhoc").ask()
    out_dir = questionary.text("Output directory:", default="downloads").ask()

    recipe = Recipe(
        name=recipe_name or "adhoc",
        region=region,
        duration_hours=duration_hours,
        categories=plans,
    )

    if not questionary.confirm("Run download now?", default=True).ask():
        console.print("[yellow]aborted[/]")
        return

    # NOMADS is throttled internally (token bucket targeting ~100/min behind
    # Akamai); GoMOFS hits S3 which has effectively no per-IP cap for our
    # volume, so it gets a much higher fan-out.
    sources = {
        "nomads": NomadsSource(max_workers=6),
        "ecmwf_open": EcmwfOpenSource(),
        "gomofs": GomofsSource(max_workers=16),
        "nwps": NwpsSource(),
    }
    result = run_recipe(
        recipe,
        registry=registry,
        sources=sources,
        dest_dir=Path(out_dir),
        log=lambda m: console.print(f"[dim]{m}[/]"),
        console=console,
    )
    console.print(f"[green]wrote[/] {result.output_grib}")
    console.print(f"[green]wrote[/] {result.manifest}")


if __name__ == "__main__":
    run_tui()
