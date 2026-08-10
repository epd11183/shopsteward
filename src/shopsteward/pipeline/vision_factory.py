"""Single construction path for VisionAdapter instances (PRD §13 decision 36):
fixture mode when not live, otherwise the provider named by
`tuning_profile.vision.provider`. Callers (CLI + API) no longer build
adapters inline."""

import os

from shopsteward.adapters.vision.fake import FixtureVisionAdapter
from shopsteward.adapters.vision.gemini import GeminiVisionAdapter
from shopsteward.adapters.vision.interface import VisionAdapter
from shopsteward.adapters.vision.openrouter import OpenRouterVisionAdapter
from shopsteward.pipeline.config import COMMERCIAL_PROMPT_PATH
from shopsteward.pipeline.models import TuningProfile


def build_vision_adapter(profile: TuningProfile, live: bool) -> VisionAdapter:
    if not live:
        return FixtureVisionAdapter()

    prompt = COMMERCIAL_PROMPT_PATH.read_text()
    provider = profile.vision.provider
    if provider == "openrouter":
        return OpenRouterVisionAdapter(
            api_key=os.environ["OPENROUTER_API_KEY"],
            prompt=prompt,
            pricing=profile.vision.est_cost_per_mtok,
        )
    if provider == "gemini":
        return GeminiVisionAdapter(
            api_key=os.environ["GEMINI_API_KEY"],
            prompt=prompt,
            pricing=profile.vision.est_cost_per_mtok,
        )
    raise ValueError(f"unknown vision provider {provider!r}")
