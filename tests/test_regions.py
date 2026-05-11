import pytest

from grib_nomad.core.regions import REGIONS, BoundingBox, get_region, parse_bbox_string


def test_builtin_regions_are_valid():
    for name, bbox in REGIONS.items():
        assert bbox.lat_min < bbox.lat_max, name
        assert bbox.lon_min < bbox.lon_max, name


def test_get_region_unknown_raises():
    with pytest.raises(KeyError):
        get_region("does-not-exist")


def test_parse_bbox_string():
    bb = parse_bbox_string("24, 45, -82, -55")
    assert bb == BoundingBox(24.0, 45.0, -82.0, -55.0)


def test_parse_bbox_bad_count():
    with pytest.raises(ValueError):
        parse_bbox_string("24, 45, -82")


def test_bbox_rejects_inverted_lats():
    with pytest.raises(ValueError):
        BoundingBox(45.0, 24.0, -82.0, -55.0)


def test_bbox_slug_is_filename_safe():
    s = BoundingBox(24, 45, -82, -55).slug()
    assert "/" not in s and " " not in s and "+" not in s
