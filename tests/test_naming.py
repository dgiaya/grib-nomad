from datetime import datetime, timezone

from grib_nomad.core.naming import build_filename, build_manifest_filename
from grib_nomad.core.recipe import CategoryPlan, ModelTier, Recipe


def _recipe() -> Recipe:
    return Recipe(
        name="bermuda-test",
        region="gulf-stream",
        duration_hours=168,
        categories=[
            CategoryPlan(
                category="wind",
                tiers=[
                    ModelTier("hrrr_conus_sfc", until_hours=48),
                    ModelTier("gfs_0p25"),
                ],
            ),
            CategoryPlan(
                category="wave",
                tiers=[ModelTier("gfs_wave_global_0p25")],
            ),
        ],
    )


def test_filename_includes_region_window_and_tier_lineage():
    aligned_start = datetime(2026, 5, 8, 18, tzinfo=timezone.utc)
    name = build_filename(_recipe(), aligned_start)
    assert name.startswith("gulf-stream_2026-05-08T18Z_d168_")
    # Aliased: hrrr_conus_sfc -> hrrrS, gfs_0p25 -> gfs, gfs_wave_global_0p25 -> gfswG
    assert "wind-hrrrS-gfs" in name
    assert "wave-gfswG" in name
    assert name.endswith(".grb2")


def test_filename_stays_under_filesystem_limit_for_kitchen_sink_recipe():
    """A 6-category recipe with multi-tier wind/gust/temp/etc. used to blow
    past the 255-byte per-component limit (ENAMETOOLONG). Aliases plus the
    hashed-summary fallback keep it bounded."""
    recipe = Recipe(
        name="everything",
        region="woods-hole",
        duration_hours=168,
        categories=[
            CategoryPlan(category="wind", tiers=[
                ModelTier("hrrr_conus_hourly"),
                ModelTier("hrrr_conus_sfc"),
                ModelTier("gfs_0p25"),
            ]),
            CategoryPlan(category="gust", tiers=[
                ModelTier("hrrr_conus_hourly"),
                ModelTier("hrrr_conus_sfc"),
                ModelTier("gfs_0p25"),
            ]),
            CategoryPlan(category="pressure", tiers=[ModelTier("gfs_0p25")]),
            CategoryPlan(category="temp_2m", tiers=[
                ModelTier("hrrr_conus_sfc"),
                ModelTier("gfs_0p25"),
            ]),
            CategoryPlan(category="reflectivity", tiers=[ModelTier("hrrr_conus_sfc")]),
            CategoryPlan(category="wave", tiers=[ModelTier("nwps_box_cg1")]),
            CategoryPlan(category="swell", tiers=[ModelTier("nwps_box_cg1")]),
            CategoryPlan(category="wind_wave", tiers=[ModelTier("nwps_box_cg1")]),
            CategoryPlan(category="current", tiers=[ModelTier("gomofs_currents")]),
        ],
    )
    aligned_start = datetime(2026, 5, 10, 14, tzinfo=timezone.utc)
    name = build_filename(recipe, aligned_start)
    # 200 is our internal cap (well below the 255 FS limit)
    assert len(name) <= 200
    assert name.startswith("woods-hole_2026-05-10T14Z_d168_")
    assert name.endswith(".grb2")


def test_filename_is_stable_across_calls():
    """Same recipe + same aligned_start -> same filename. Important so a
    re-run of a saved recipe overwrites the previous output rather than
    accumulating duplicates."""
    aligned_start = datetime(2026, 5, 8, 18, tzinfo=timezone.utc)
    n1 = build_filename(_recipe(), aligned_start)
    n2 = build_filename(_recipe(), aligned_start)
    assert n1 == n2


def test_manifest_sidecar_name():
    assert build_manifest_filename("foo.grb2") == "foo.manifest.json"
    assert build_manifest_filename("a.b.c.grb2") == "a.b.c.manifest.json"
