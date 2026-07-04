"""Paths to shippable defaults + a tiny loader for editing.json."""

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
PRESET_FAMILIES_DIR = _REPO_ROOT / "config" / "defaults" / "preset_families"
EDITING_DEFAULTS_PATH = _REPO_ROOT / "config" / "defaults" / "editing.json"


def load_editing_defaults() -> dict:
    return json.loads(EDITING_DEFAULTS_PATH.read_text())
