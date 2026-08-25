"""Paths to shippable defaults + a tiny loader for editing.json."""

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
PRESET_FAMILIES_DIR = _REPO_ROOT / "config" / "defaults" / "preset_families"
EDITING_DEFAULTS_PATH = _REPO_ROOT / "config" / "defaults" / "editing.json"


def load_editing_defaults() -> dict:
    # encoding="utf-8" explicit (M4, guardrail review 2026-08-25) --
    # `read_text()`'s platform-default encoding is cp1252 on Windows.
    return json.loads(EDITING_DEFAULTS_PATH.read_text(encoding="utf-8"))


LOOKS_DIR = _REPO_ROOT / "config" / "defaults" / "looks"
LOOK_PROMPT_PATH = _REPO_ROOT / "config" / "defaults" / "prompts" / "look_profile.txt"


def load_correction_knobs() -> dict:
    return load_editing_defaults().get("correction", {})


def load_look_prompt() -> str:
    # encoding="utf-8" explicit (M4, guardrail review 2026-08-25) -- a
    # PROMPT file sent to an LLM; a silent cp1252 mojibake corruption on
    # Windows changes the prompt without raising.
    return LOOK_PROMPT_PATH.read_text(encoding="utf-8")


def load_look_llm() -> dict:
    return load_editing_defaults().get("look_llm", {})


def load_look_guard() -> dict:
    return load_editing_defaults().get("look_guard", {})
