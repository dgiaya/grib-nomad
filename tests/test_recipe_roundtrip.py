from datetime import datetime, timezone

from grib_nomad.core.recipe import CategoryPlan, ModelTier, Recipe
from grib_nomad.core.regions import BoundingBox


def test_recipe_named_region_roundtrip():
    r = Recipe(
        name="x",
        region="gulf-stream",
        duration_hours=168,
        categories=[
            CategoryPlan(
                category="wind",
                tiers=[
                    ModelTier("hrrr_conus_sfc", until_hours=48),
                    ModelTier("gfs_0p25"),
                ],
            )
        ],
    )
    r2 = Recipe.from_dict(r.to_dict())
    assert r2 == r


def test_recipe_custom_bbox_and_start_roundtrip():
    r = Recipe(
        name="custom",
        region=BoundingBox(24.0, 45.0, -82.0, -55.0),
        duration_hours=72,
        start=datetime(2026, 5, 8, 14, tzinfo=timezone.utc),
        categories=[
            CategoryPlan(
                category="wave",
                tiers=[ModelTier("gfs_wave_global_0p25", step_hours=6)],
            )
        ],
    )
    r2 = Recipe.from_dict(r.to_dict())
    assert r2.region == r.region
    assert r2.start == r.start
    assert r2.duration_hours == 72
    assert r2.categories == r.categories
