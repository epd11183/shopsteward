"""Paths to shippable defaults + a tiny loader for editing.json."""

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
PRESET_FAMILIES_DIR = _REPO_ROOT / "config" / "defaults" / "preset_families"
EDITING_DEFAULTS_PATH = _REPO_ROOT / "config" / "defaults" / "editing.json"


def load_editing_defaults() -> dict:
    return json.loads(EDITING_DEFAULTS_PATH.read_text())


LOOKS_DIR = _REPO_ROOT / "config" / "defaults" / "looks"
LOOK_PROMPT_PATH = _REPO_ROOT / "config" / "defaults" / "prompts" / "look_profile.txt"


def load_correction_knobs() -> dict:
    return load_editing_defaults().get("correction", {})


def load_look_prompt() -> str:
    return LOOK_PROMPT_PATH.read_text()


def load_look_llm() -> dict:
    return load_editing_defaults().get("look_llm", {})


def load_look_guard() -> dict:
    return load_editing_defaults().get("look_guard", {})
