"""GRIB2 byte-stream concatenation and sidecar-manifest writing.

A GRIB2 file is a sequence of self-describing messages; concatenating message
streams from multiple sources is a valid GRIB2 file. We do a light sanity check
(every part starts with the GRIB magic) but do not parse messages — that would
require pygrib/cfgrib and is not needed for the routing-prep workflow.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from grib_nomad.core.recipe import Recipe

GRIB2_MAGIC = b"GRIB"


@dataclass
class DownloadedPart:
    path: Path
    model_id: str
    category: str
    cycle: datetime
    fhour: int
    bytes_: int
    source_url: str
    bbox: dict = field(default_factory=dict)
    variables: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)


def _looks_like_grib2(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) == GRIB2_MAGIC


def concatenate_gribs(parts: list[DownloadedPart], dest: Path) -> Path:
    """Concatenate part files into a single GRIB2 at `dest`. Returns `dest`."""
    if not parts:
        raise ValueError("no parts to concatenate")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        for part in parts:
            if not part.path.exists():
                raise FileNotFoundError(part.path)
            if part.bytes_ > 0 and not _looks_like_grib2(part.path):
                raise ValueError(
                    f"{part.path} does not start with GRIB2 magic; "
                    "the upstream server may have returned an error page"
                )
            with part.path.open("rb") as f:
                while chunk := f.read(1024 * 1024):
                    out.write(chunk)
    return dest


def write_manifest(
    recipe: Recipe,
    init: datetime,
    parts: list[DownloadedPart],
    dest: Path,
    output_grib: Path,
    extras: dict | None = None,
) -> Path:
    """Write a JSON manifest documenting every part of a combined GRIB."""
    bbox = recipe.resolve_region()
    payload = {
        "schema_version": 1,
        "tool": "grib_nomad",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "recipe": recipe.to_dict(),
        "init_cycle": init.isoformat(timespec="seconds") + "Z",
        "region_bbox": bbox.to_dict(),
        "output_grib": output_grib.name,
        "parts": [_part_to_json(p) for p in parts],
    }
    if extras:
        payload["extras"] = extras
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2, default=str))
    return dest


def _part_to_json(p: DownloadedPart) -> dict:
    d = asdict(p)
    d["path"] = str(p.path)
    d["cycle"] = p.cycle.isoformat(timespec="seconds") + "Z"
    return d
