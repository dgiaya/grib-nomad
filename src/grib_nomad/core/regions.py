"""Bounding-box regions: built-in presets plus parsing helpers.

Longitudes use the -180..180 convention internally. NOMADS grib_filter happens to
accept either -180..180 or 0..360 for many models; conversion is done where it matters.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoundingBox:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    name: str | None = None

    def __post_init__(self) -> None:
        if not -90 <= self.lat_min < self.lat_max <= 90:
            raise ValueError(f"invalid latitudes: {self.lat_min}..{self.lat_max}")
        if not -180 <= self.lon_min <= 180 or not -180 <= self.lon_max <= 180:
            raise ValueError(f"invalid longitudes: {self.lon_min}..{self.lon_max}")
        if self.lon_min == self.lon_max:
            raise ValueError("lon_min and lon_max must differ")

    def as_lon_360(self) -> tuple[float, float]:
        """Return (left, right) in 0..360 longitude. Handles dateline crossings as a wrap."""
        left = self.lon_min % 360
        right = self.lon_max % 360
        return left, right

    def slug(self) -> str:
        """Short, filename-safe identifier for this bbox."""
        if self.name:
            return self.name
        return (
            f"lat{self.lat_min:+.0f}to{self.lat_max:+.0f}_"
            f"lon{self.lon_min:+.0f}to{self.lon_max:+.0f}"
        ).replace("+", "p").replace("-", "m")

    def to_dict(self) -> dict:
        return {
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BoundingBox:
        return cls(
            lat_min=float(d["lat_min"]),
            lat_max=float(d["lat_max"]),
            lon_min=float(d["lon_min"]),
            lon_max=float(d["lon_max"]),
            name=d.get("name"),
        )


# Built-in regions, biased toward US East Coast / Gulf Stream / Caribbean routing.
# Bounds are generous; trim per-run for smaller downloads.
REGIONS: dict[str, BoundingBox] = {
    "gulf-stream": BoundingBox(24.0, 45.0, -82.0, -55.0, "gulf-stream"),
    "bermuda-triangle": BoundingBox(18.0, 38.0, -78.0, -55.0, "bermuda-triangle"),
    "caribbean": BoundingBox(8.0, 25.0, -90.0, -58.0, "caribbean"),
    "bahamas": BoundingBox(20.0, 28.0, -82.0, -72.0, "bahamas"),
    "new-england-offshore": BoundingBox(38.0, 45.0, -75.0, -60.0, "new-england-offshore"),
    "florida-straits": BoundingBox(22.0, 28.0, -84.0, -75.0, "florida-straits"),
    "eastcoast-wide": BoundingBox(20.0, 48.0, -85.0, -50.0, "eastcoast-wide"),
    "north-atlantic": BoundingBox(20.0, 60.0, -80.0, -10.0, "north-atlantic"),
    # GoMOFS native domain (NOAA CO-OPS Gulf of Maine OFS)
    "gulf-of-maine": BoundingBox(40.0, 45.5, -71.0, -65.5, "gulf-of-maine"),
    "gulf-of-maine-wide": BoundingBox(39.0, 46.0, -72.0, -64.0, "gulf-of-maine-wide"),
    # ~10 NM box around Woods Hole, MA (41.526°N, -70.671°W) — small, fast,
    # exercises the GoMOFS coastal masking + rotation paths.
    "woods-hole": BoundingBox(41.35, 41.70, -70.90, -70.45, "woods-hole"),
}


def get_region(name: str) -> BoundingBox:
    try:
        return REGIONS[name]
    except KeyError as e:
        valid = ", ".join(sorted(REGIONS))
        raise KeyError(f"unknown region {name!r}; valid: {valid}") from e


def parse_bbox_string(s: str) -> BoundingBox:
    """Parse a 'lat_min,lat_max,lon_min,lon_max' string into a BoundingBox."""
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise ValueError(
            f"expected 'lat_min,lat_max,lon_min,lon_max', got {s!r}"
        )
    lat_min, lat_max, lon_min, lon_max = (float(p) for p in parts)
    return BoundingBox(lat_min, lat_max, lon_min, lon_max)
