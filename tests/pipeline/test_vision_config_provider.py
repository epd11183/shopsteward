import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from shopsteward.pipeline.models import TuningProfile

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


def test_default_profile_provider_is_openrouter() -> None:
    profile = TuningProfile.model_validate(json.loads(DEFAULTS_PATH.read_text()))
    assert profile.vision.provider == "openrouter"


def test_missing_provider_field_defaults_to_openrouter() -> None:
    """Profiles stored before decision 36 lack `provider` in their payload;
    they must still parse, defaulting to openrouter."""
    raw = json.loads(DEFAULTS_PATH.read_text())
    del raw["vision"]["provider"]
    profile = TuningProfile.model_validate(raw)
    assert profile.vision.provider == "openrouter"


def test_gemini_provider_is_valid() -> None:
    raw = json.loads(DEFAULTS_PATH.read_text())
    raw["vision"]["provider"] = "gemini"
    profile = TuningProfile.model_validate(raw)
    assert profile.vision.provider == "gemini"


def test_invalid_provider_raises_validation_error() -> None:
    raw = json.loads(DEFAULTS_PATH.read_text())
    raw["vision"]["provider"] = "openai"
    with pytest.raises(ValidationError):
        TuningProfile.model_validate(raw)
