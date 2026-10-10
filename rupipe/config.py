"""Config loading: config.yaml, then config.local.yaml if present, then --config.

config.yaml holds portable defaults. config.local.yaml (next to it, not shared)
holds the settings of one machine, such as tool paths and working folders.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"
LOCAL_CONFIG = PROJECT_ROOT / "config.local.yaml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(override_path: str | Path | None = None) -> dict:
    cfg = _load(DEFAULT_CONFIG)
    if LOCAL_CONFIG.is_file():
        cfg = _merge(cfg, _load(LOCAL_CONFIG))
    if override_path:
        cfg = _merge(cfg, _load(Path(override_path)))
    return cfg
