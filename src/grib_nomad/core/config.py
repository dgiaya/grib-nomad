"""User-config locations and YAML persistence helpers for favorites."""

from __future__ import annotations

from pathlib import Path

import yaml
from platformdirs import user_config_path

APP_NAME = "grib_nomad"


def config_dir() -> Path:
    p = user_config_path(APP_NAME, appauthor=False, ensure_exists=True)
    return Path(p)


def presets_path() -> Path:
    return config_dir() / "presets.yaml"


def recipes_path() -> Path:
    return config_dir() / "recipes.yaml"


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
