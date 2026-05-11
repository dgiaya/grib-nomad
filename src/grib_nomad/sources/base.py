"""Source/Model abstractions.

A `Source` is one upstream server or service (e.g. NOMADS, ECMWF Open Data). A
`ModelSpec` is a forecast product hosted by a source (e.g. GFS 0.25°). Sources own
the URL-construction and HTTP details; the rest of the codebase deals only in
ModelSpecs and DownloadRequests so the same recipe-running logic works regardless
of where the data ultimately comes from.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from grib_nomad.core.combine import DownloadedPart
from grib_nomad.core.regions import BoundingBox

PartCallback = Callable[[DownloadedPart], None]
StatusCallback = Callable[[str], None]


class SourceError(RuntimeError):
    """Any failure in a Source: registry lookup, URL construction, HTTP, etc."""


@dataclass
class CategoryVars:
    """Variables and levels that realize a category for one model."""

    vars: tuple[str, ...]
    levels: tuple[str, ...]


@dataclass
class StepRule:
    from_hour: int
    to_hour: int
    step: int


@dataclass
class ModelSpec:
    id: str
    display_name: str
    source: str
    cycles: tuple[int, ...]
    max_fhour: int
    steps: tuple[StepRule, ...]
    categories: dict[str, CategoryVars]
    latency_hours: int = 4
    """Approx publish delay after the cycle hour. Used to pick 'latest available'."""

    extra: dict = field(default_factory=dict)
    """Source-specific config (URL templates for NOMADS, S3 prefixes for ECMWF, etc.)."""

    def covers_category(self, category: str) -> bool:
        return category in self.categories


@dataclass
class DownloadRequest:
    """One model + category + (region, cycle, fhours) bundle to fetch."""

    model: ModelSpec
    category: str
    bbox: BoundingBox
    cycle_date: date
    cycle_hour: int
    fhours: list[int]


class Source(ABC):
    """Plugin interface for a forecast-data provider."""

    name: str

    @abstractmethod
    def download(
        self,
        request: DownloadRequest,
        dest_dir: Path,
        *,
        on_part: PartCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> list[DownloadedPart]:
        """Fetch all forecast hours in `request` to files under `dest_dir`.

        Implementations should fetch in parallel where useful and call `on_part`
        (if provided) once per part as it completes — the runner uses this hook
        to drive a progress bar.

        `on_status` (if provided) lets long-running setup phases — e.g.
        GoMOFS' one-time HDF5 metadata walk — surface what they're doing
        before any forecast-hour parts have completed; it takes a single
        short status string. NOMADS sources won't typically need it.

        The returned list is sorted by `fhour` for deterministic combine order.
        """
        raise NotImplementedError

    def latest_ready_cycle(self, model: ModelSpec, *, now: datetime | None = None) -> datetime:
        """Most recent cycle whose data should be fully published.

        Default heuristic: walk backwards from `now` (UTC) and pick the first cycle
        hour in `model.cycles` whose publish-by time (cycle + latency_hours) is in
        the past. Subclasses may override to query the server for actual availability.
        """
        from datetime import timedelta

        now = now or datetime.utcnow()
        cursor = now.replace(minute=0, second=0, microsecond=0)
        for _ in range(48):
            if cursor.hour in model.cycles:
                ready_at = cursor + timedelta(hours=model.latency_hours)
                if ready_at <= now:
                    return cursor
            cursor -= timedelta(hours=1)
        raise SourceError(
            f"could not find a ready cycle for {model.id} within 48h of {now.isoformat()}"
        )
