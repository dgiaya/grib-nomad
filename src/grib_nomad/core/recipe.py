"""Recipe / Preset / CategoryPlan dataclasses.

Recipes describe a download in terms the user actually thinks in:

  - When does the forecast start? (`start` — defaults to "now")
  - How long should it cover? (`duration_hours`)
  - For each data category (wind, wave, current, …), which models cover which
    *time slices* of that duration? Tiers are sequential: tier 1 covers from
    `start` to `start + tier1.until_hours`, tier 2 picks up from there, etc.
  - On what time grid should the output land? (`step_hours` — used as a
    per-tier override only; the runner builds the actual master grid as the
    union of every tier's native valid_times.)

No more thinking about model cycles or forecast hours — the runner picks the
freshest ready cycle for each model and translates the time slices into fhours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from grib_nomad.core.regions import REGIONS, BoundingBox


@dataclass
class ModelTier:
    """One model's slice within a category plan, expressed in hours from `start`.

    `until_hours=None` means "cover the rest of the recipe duration" (or the
    rest of what the model can actually provide, whichever is shorter).
    `step_hours=None` means "use the model's native step within this slice".
    """

    model_id: str
    until_hours: int | None = None
    step_hours: int | None = None

    def to_dict(self) -> dict:
        d: dict = {"model_id": self.model_id}
        if self.until_hours is not None:
            d["until_hours"] = self.until_hours
        if self.step_hours is not None:
            d["step_hours"] = self.step_hours
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ModelTier:
        return cls(
            model_id=str(d["model_id"]),
            until_hours=int(d["until_hours"]) if d.get("until_hours") is not None else None,
            step_hours=int(d["step_hours"]) if d.get("step_hours") is not None else None,
        )


@dataclass
class CategoryPlan:
    category: str
    tiers: list[ModelTier] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"category": self.category, "tiers": [t.to_dict() for t in self.tiers]}

    @classmethod
    def from_dict(cls, d: dict) -> CategoryPlan:
        return cls(
            category=str(d["category"]),
            tiers=[ModelTier.from_dict(t) for t in d.get("tiers", [])],
        )


@dataclass
class Recipe:
    name: str
    region: str | BoundingBox
    duration_hours: int = 168
    """How long to cover (default 7 days)."""

    start: datetime | None = None
    """When the forecast window begins. `None` = "now" at runtime, in UTC.
    If timezone-aware, will be converted to UTC. If naive, interpreted as local TZ."""

    categories: list[CategoryPlan] = field(default_factory=list)

    def resolve_region(self) -> BoundingBox:
        if isinstance(self.region, BoundingBox):
            return self.region
        return REGIONS[self.region]

    def to_dict(self) -> dict:
        if isinstance(self.region, BoundingBox):
            region_payload: str | dict = self.region.to_dict()
        else:
            region_payload = self.region
        d: dict = {
            "name": self.name,
            "region": region_payload,
            "duration_hours": self.duration_hours,
            "categories": [c.to_dict() for c in self.categories],
        }
        if self.start is not None:
            d["start"] = self.start.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Recipe:
        region = d["region"]
        if isinstance(region, dict):
            region = BoundingBox.from_dict(region)
        start = d.get("start")
        start_dt = datetime.fromisoformat(start) if start else None
        return cls(
            name=str(d["name"]),
            region=region,
            duration_hours=int(d.get("duration_hours", 168)),
            start=start_dt,
            categories=[CategoryPlan.from_dict(c) for c in d.get("categories", [])],
        )


@dataclass
class Preset:
    """A reusable named piece — region, model-per-category mapping, or full tier ladder."""

    name: str
    kind: str
    payload: dict

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict) -> Preset:
        return cls(name=str(d["name"]), kind=str(d["kind"]), payload=dict(d["payload"]))
