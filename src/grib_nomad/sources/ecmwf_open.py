"""ECMWF Open Data source — placeholder for v0.

ECMWF makes the IFS open data available for free at https://data.ecmwf.int/forecasts/
under the CC BY 4.0 license. Layout differs from NOMADS:

  https://data.ecmwf.int/forecasts/{YYYYMMDD}/{HH}z/ifs/0p25/oper/{YYYYMMDD}{HH}0000-{step}h-oper-fc.grib2

There is no per-variable filter endpoint analogous to NOMADS grib_filter, so a real
implementation will:

  1. Download the full GRIB2 file for the (date, cycle, step).
  2. Filter messages locally by parameter name / level / bbox using either pygrib or
     a hand-rolled GRIB2 message walker.
  3. Concatenate the filtered messages.

This stub raises NotImplementedError so the registry loader can still see the
source name and ModelSpecs without breaking imports.
"""

from __future__ import annotations

from pathlib import Path

from grib_nomad.core.combine import DownloadedPart
from grib_nomad.sources.base import (
    DownloadRequest,
    PartCallback,
    Source,
    SourceError,
    StatusCallback,
)


class EcmwfOpenSource(Source):
    name = "ecmwf_open"

    BASE_URL = "https://data.ecmwf.int/forecasts"

    def download(
        self,
        request: DownloadRequest,
        dest_dir: Path,
        *,
        on_part: PartCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[DownloadedPart]:
        raise SourceError(
            "ECMWF Open Data source is not implemented yet; tracked as a roadmap item. "
            "See sources/ecmwf_open.py for the planned approach."
        )
