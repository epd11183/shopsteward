"""Paths to shippable pipeline defaults. Mirrors editing/config.py."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
TUNING_PROFILE_PATH = _REPO_ROOT / "config" / "defaults" / "tuning_profile.json"
COMMERCIAL_PROMPT_PATH = _REPO_ROOT / "config" / "defaults" / "prompts" / "commercial_score.txt"
