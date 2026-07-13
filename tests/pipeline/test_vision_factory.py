import json
from pathlib import Path

from shopsteward.adapters.vision.fake import FixtureVisionAdapter
from shopsteward.adapters.vision.gemini import GeminiVisionAdapter
from shopsteward.adapters.vision.openrouter import OpenRouterVisionAdapter
from shopsteward.pipeline.models import TuningProfile
from shopsteward.pipeline.vision_factory import build_vision_adapter

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


def _profile(provider: str) -> TuningProfile:
    raw = json.loads(DEFAULTS_PATH.read_text())
    raw["vision"]["provider"] = provider
    return TuningProfile.model_validate(raw)


def test_build_vision_adapter_not_live_returns_fixture_regardless_of_provider() -> None:
    for provider in ("openrouter", "gemini"):
        adapter = build_vision_adapter(_profile(provider), live=False)
        assert isinstance(adapter, FixtureVisionAdapter)


def test_build_vision_adapter_live_openrouter(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    adapter = build_vision_adapter(_profile("openrouter"), live=True)
    assert isinstance(adapter, OpenRouterVisionAdapter)


def test_build_vision_adapter_live_gemini(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gm-key")
    adapter = build_vision_adapter(_profile("gemini"), live=True)
    assert isinstance(adapter, GeminiVisionAdapter)
