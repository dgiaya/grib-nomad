"""URL-construction tests for the NOMADS source. No network."""

from datetime import date

from grib_nomad.core.regions import BoundingBox
from grib_nomad.models import load_registry
from grib_nomad.sources.base import DownloadRequest
from grib_nomad.sources.nomads import NomadsSource


def _request(model_id: str, category: str, fhour_for_eq: int) -> DownloadRequest:
    registry = load_registry()
    return DownloadRequest(
        model=registry[model_id],
        category=category,
        bbox=BoundingBox(24.0, 45.0, -82.0, -55.0),
        cycle_date=date(2026, 5, 8),
        cycle_hour=18,
    fhours=[fhour_for_eq],
    )


def test_gfs_url_and_params():
    src = NomadsSource()
    req = _request("gfs_0p25", "wind", 24)
    url, params = src.build_url_and_params(req, fhour=24)
    assert url == "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
    assert params["dir"] == "/gfs.20260508/18/atmos"
    assert params["file"] == "gfs.t18z.pgrb2.0p25.f024"
    assert params["var_UGRD"] == "on"
    assert params["var_VGRD"] == "on"
    assert params["lev_10_m_above_ground"] == "on"
    assert params["toplat"] == "45.0"
    assert params["bottomlat"] == "24.0"
    assert params["leftlon"] == "-82.0"
    assert params["rightlon"] == "-55.0"


def test_hrrr_url_uses_two_digit_fhour():
    src = NomadsSource()
    req = _request("hrrr_conus_sfc", "wind", 6)
    url, params = src.build_url_and_params(req, fhour=6)
    assert "filter_hrrr_2d.pl" in url
    assert params["file"] == "hrrr.t18z.wrfsfcf06.grib2"
    assert params["dir"] == "/hrrr.20260508/conus"


def test_gfs_wave_uses_wave_directory():
    src = NomadsSource()
    req = _request("gfs_wave_global_0p25", "wave", 12)
    url, params = src.build_url_and_params(req, fhour=12)
    assert "filter_gfswave.pl" in url
    assert params["dir"] == "/gfs.20260508/18/wave/gridded"
    assert params["file"] == "gfswave.t18z.global.0p25.f012.grib2"
    assert params["var_HTSGW"] == "on"
