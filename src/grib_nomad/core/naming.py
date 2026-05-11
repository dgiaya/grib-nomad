"""Output filename construction.

The filename encodes everything needed to disambiguate one run from another at a
glance: region, window start (UTC), duration, and per-category model lineage.
The companion manifest sidecar carries full provenance.

Example:
    gulf-of-maine-wide_2026-05-08T18Z_d168_wind-hrrrS-gfs_wave-gfswG_current-gomofs.grb2

Model ids are abbreviated to short tokens (see `MODEL_ALIASES`) so that recipes
with many categories and tiers stay under the filesystem's 255-byte per-component
limit (macOS HFS+/APFS, ext4, NTFS). The manifest still records the full model
ids, so nothing is lost — the filename is for human glance, the manifest is for
provenance.

If aliasing still leaves the filename too long (e.g. an exotic recipe with a
dozen tiers), `build_filename` falls back to a hashed summary instead of letting
the filesystem reject the path.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from grib_nomad.core.recipe import CategoryPlan, Recipe

# Compact model aliases used in output filenames. Keep these stable; renaming
# breaks how saved-recipe runs find their previous outputs (humans only, the
# cache uses model_id not alias).
MODEL_ALIASES: dict[str, str] = {
    "hrrr_conus_hourly": "hrrrH",
    "hrrr_conus_sfc": "hrrrS",
    "gfs_0p25": "gfs",
    "gfs_wave_atlantic_0p16": "gfswA",
    "gfs_wave_global_0p25": "gfswG",
    "nam_conusnest": "namC",
    "nam_12km": "nam12",
    "gomofs_currents": "gomofs",
    # NWPS WFO grids — `nwpsBOX` style stays compact even if we add more WFOs.
    "nwps_box_cg1": "nwpsBOX",
}

# Defensive cap: 255 is the per-component filesystem limit on macOS/Linux,
# but many tools (zip, scp, sync) get unhappy well before that. 200 gives
# us headroom for the ".manifest.json" sidecar suffix and for the user's
# own renames.
_MAX_FILENAME_LEN = 200


def _fmt_cycle(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%HZ")


def _short_model_token(model_id: str) -> str:
    """Compact alias for use in filenames. Falls back to dash-form for
    model ids we haven't curated an alias for yet."""
    return MODEL_ALIASES.get(model_id, model_id.replace("_", "-"))


def _category_segment(plan: CategoryPlan) -> str:
    parts = [_short_model_token(t.model_id) for t in plan.tiers]
    return f"{plan.category}-" + "-".join(parts)


def build_filename(recipe: Recipe, aligned_start: datetime, ext: str = "grb2") -> str:
    region_slug = recipe.resolve_region().slug()
    cycle_slug = _fmt_cycle(aligned_start)
    duration_tag = f"d{recipe.duration_hours}"
    category_slugs = "_".join(_category_segment(c) for c in recipe.categories)
    full = f"{region_slug}_{cycle_slug}_{duration_tag}_{category_slugs}.{ext}"
    if len(full) <= _MAX_FILENAME_LEN:
        return full

    # Fallback: the category list is the only piece that's unbounded. Replace
    # it with a short, deterministic hash so the filename stays stable across
    # runs of the same recipe but stays under the FS limit. Full provenance
    # is still in the manifest sidecar.
    cat_count = len(recipe.categories)
    tier_count = sum(len(c.tiers) for c in recipe.categories)
    hash_tag = hashlib.sha256(category_slugs.encode("utf-8")).hexdigest()[:8]
    return (
        f"{region_slug}_{cycle_slug}_{duration_tag}_"
        f"{cat_count}cat-{tier_count}tier_h{hash_tag}.{ext}"
    )


def build_manifest_filename(grib_filename: str) -> str:
    if "." in grib_filename:
        stem, _, _ = grib_filename.rpartition(".")
    else:
        stem = grib_filename
    return f"{stem}.manifest.json"
