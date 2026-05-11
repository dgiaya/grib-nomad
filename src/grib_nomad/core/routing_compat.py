"""GRIB2 parameter codes that mainstream weather-routing software recognizes.

Every routing app (OpenCPN, qtVlm, XyGrib, Expedition, Squid, Predict Wind, …)
keys off the **(discipline, parameterCategory, parameterNumber)** triple, not
the eccodes `shortName`. The triple is the WMO standard; eccodes ships an
incomplete name lookup (e.g. for ocean currents — see the project memory note),
so a message can render as `shortName=unknown` in `grib_ls` and yet display
correctly in qtVlm because qtVlm has its own table.

Entries here are the codes those apps actually look for. Source-of-truth is
the WMO GRIB2 code-flag tables (https://codes.wmo.int/grib2/codeflag/4.2 and
its sub-tables) cross-referenced against:
  - OpenCPN GRIB plugin source (`grib_pi` enum values)
  - qtVlm parameter list
  - XyGrib's documented variable list

`recognized_by` is conservative — listed apps definitely handle the code; some
others might too. `notes` flags any common gotchas.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParamSpec:
    short: str          # NCEP / WMO short identifier
    long_name: str      # human-readable
    units: str
    recognized_by: tuple[str, ...]
    notes: str = ""


# (discipline, parameterCategory, parameterNumber) -> ParamSpec
ROUTING_PARAMS: dict[tuple[int, int, int], ParamSpec] = {
    # --- Discipline 0: Meteorology ----------------------------------------

    # Category 0: Temperature
    (0, 0, 0):  ParamSpec("TMP", "Temperature", "K",
                          ("OpenCPN", "qtVlm", "XyGrib")),
    (0, 0, 4):  ParamSpec("TMAX", "Maximum temperature", "K",
                          ("qtVlm",)),
    (0, 0, 5):  ParamSpec("TMIN", "Minimum temperature", "K",
                          ("qtVlm",)),

    # Category 1: Moisture
    (0, 1, 1):  ParamSpec("RH", "Relative humidity", "%",
                          ("OpenCPN", "qtVlm", "XyGrib")),
    (0, 1, 7):  ParamSpec("PRATE", "Precipitation rate", "kg m-2 s-1",
                          ("OpenCPN", "qtVlm", "XyGrib"),
                          "rate; multiply by 3600 for mm/h"),
    (0, 1, 8):  ParamSpec("APCP", "Total precipitation (accumulated)", "kg m-2",
                          ("OpenCPN", "qtVlm", "XyGrib")),
    (0, 1, 11): ParamSpec("SNOD", "Snow depth", "m",
                          ("qtVlm",)),

    # Category 2: Momentum (winds)
    (0, 2, 2):  ParamSpec("UGRD", "U-component of wind", "m s-1",
                          ("OpenCPN", "qtVlm", "XyGrib"),
                          "level 103/10 m -> '10 m wind' in routing UI"),
    (0, 2, 3):  ParamSpec("VGRD", "V-component of wind", "m s-1",
                          ("OpenCPN", "qtVlm", "XyGrib")),
    (0, 2, 22): ParamSpec("GUST", "Wind speed (gust)", "m s-1",
                          ("OpenCPN", "qtVlm", "XyGrib")),
    (0, 2, 1):  ParamSpec("WIND", "Wind speed", "m s-1",
                          ("OpenCPN", "qtVlm")),
    (0, 2, 0):  ParamSpec("WDIR", "Wind direction (from)", "degree true",
                          ("OpenCPN", "qtVlm")),

    # Category 3: Mass (pressure)
    (0, 3, 0):  ParamSpec("PRES", "Pressure", "Pa",
                          ("OpenCPN", "qtVlm")),
    (0, 3, 1):  ParamSpec("PRMSL", "Pressure reduced to MSL", "Pa",
                          ("OpenCPN", "qtVlm", "XyGrib"),
                          "isobars / surface pressure on most routing maps"),

    # Category 6: Cloud
    (0, 6, 1):  ParamSpec("TCDC", "Total cloud cover", "%",
                          ("OpenCPN", "qtVlm", "XyGrib")),

    # Category 7: Thermodynamic stability
    (0, 7, 6):  ParamSpec("CAPE", "Convective available potential energy",
                          "J kg-1", ("qtVlm",)),

    # --- Discipline 10: Oceanography --------------------------------------

    # Category 0: Waves
    (10, 0, 3):  ParamSpec("HTSGW",
                           "Significant height of combined wind waves and swell",
                           "m", ("OpenCPN", "qtVlm", "XyGrib"),
                           "primary 'wave height' field for routing"),
    (10, 0, 11): ParamSpec("PERPW", "Primary wave mean period", "s",
                           ("OpenCPN", "qtVlm", "XyGrib")),
    (10, 0, 10): ParamSpec("DIRPW", "Primary wave direction", "degree true",
                           ("OpenCPN", "qtVlm", "XyGrib")),
    (10, 0, 5):  ParamSpec("WVHGT", "Significant height of wind waves", "m",
                           ("OpenCPN", "qtVlm"),
                           "wind-wave only (excludes swell)"),
    (10, 0, 6):  ParamSpec("WVPER", "Mean period of wind waves", "s",
                           ("OpenCPN", "qtVlm")),
    (10, 0, 4):  ParamSpec("WVDIR", "Direction of wind waves", "degree true",
                           ("OpenCPN", "qtVlm")),
    (10, 0, 8):  ParamSpec("SWELL", "Significant height of swell waves", "m",
                           ("OpenCPN", "qtVlm")),
    (10, 0, 9):  ParamSpec("SWPER", "Mean period of swell waves", "s",
                           ("OpenCPN", "qtVlm")),
    (10, 0, 7):  ParamSpec("SWDIR", "Direction of swell waves", "degree true",
                           ("OpenCPN", "qtVlm")),

    # Category 1: Currents
    (10, 1, 0):  ParamSpec("DIRC", "Current direction", "degree true",
                           ("OpenCPN", "qtVlm")),
    (10, 1, 1):  ParamSpec("SPC", "Current speed", "m s-1",
                           ("OpenCPN", "qtVlm")),
    (10, 1, 2):  ParamSpec("UOGRD", "U-component of current", "m s-1",
                           ("OpenCPN", "qtVlm"),
                           "eccodes <2.x ships no shortName -> displays as "
                           "'unknown' in grib_ls; routing apps still recognize "
                           "the (10,1,2) triple"),
    (10, 1, 3):  ParamSpec("VOGRD", "V-component of current", "m s-1",
                           ("OpenCPN", "qtVlm"), "see UOGRD note"),

    # Category 3: Surface properties
    (10, 3, 0):  ParamSpec("WTMP", "Water temperature", "K",
                           ("OpenCPN", "qtVlm")),
}


def lookup(discipline: int, param_cat: int, param_num: int) -> ParamSpec | None:
    return ROUTING_PARAMS.get((discipline, param_cat, param_num))
