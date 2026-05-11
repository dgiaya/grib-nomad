"""Model registry: load ModelSpecs from the bundled YAML files and look them up."""

from __future__ import annotations

from pathlib import Path

import yaml

from grib_nomad.sources.base import CategoryVars, ModelSpec, StepRule


def _packaged_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def load_registry(paths: list[Path] | Path | None = None) -> dict[str, ModelSpec]:
    """Parse one or more registry YAML files into a {model_id: ModelSpec} dict.

    By default loads every `*_models.yaml` file in the packaged `data/` directory,
    so adding a new source is a matter of dropping in a new YAML file.
    """
    if paths is None:
        files_to_load = sorted(_packaged_data_dir().glob("*_models.yaml"))
    elif isinstance(paths, Path):
        files_to_load = [paths]
    else:
        files_to_load = list(paths)
    out: dict[str, ModelSpec] = {}
    for p in files_to_load:
        raw = yaml.safe_load(p.read_text()) or {}
        for entry in raw.get("models", []):
            spec = _spec_from_yaml(entry)
            if spec.id in out:
                raise ValueError(
                    f"duplicate model id {spec.id!r} (also defined in {p})"
                )
            out[spec.id] = spec
    return out


def _spec_from_yaml(entry: dict) -> ModelSpec:
    steps = tuple(
        StepRule(int(s["from"]), int(s["to"]), int(s["step"])) for s in entry.get("steps", [])
    )
    cats: dict[str, CategoryVars] = {}
    for cname, cvars in (entry.get("categories") or {}).items():
        cats[cname] = CategoryVars(
            vars=tuple(cvars.get("vars", [])),
            levels=tuple(cvars.get("levels", [])),
        )
    return ModelSpec(
        id=str(entry["id"]),
        display_name=str(entry.get("display_name", entry["id"])),
        source=str(entry["source"]),
        cycles=tuple(int(c) for c in entry.get("cycles", [])),
        max_fhour=int(entry.get("max_fhour", 0)),
        steps=steps,
        categories=cats,
        latency_hours=int(entry.get("latency_hours", 4)),
        extra=dict(entry.get("extra", {})),
    )


def models_for_category(registry: dict[str, ModelSpec], category: str) -> list[ModelSpec]:
    return [m for m in registry.values() if m.covers_category(category)]


def get_model(registry: dict[str, ModelSpec], model_id: str) -> ModelSpec:
    if model_id not in registry:
        valid = ", ".join(sorted(registry))
        raise KeyError(f"unknown model {model_id!r}; known: {valid}")
    return registry[model_id]
