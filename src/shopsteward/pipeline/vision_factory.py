"""Single construction path for VisionAdapter instances (PRD §13 decision 36):
fixture mode when not live, otherwise the provider named by
`tuning_profile.vision.provider`. Callers (CLI + API) no longer build
adapters inline."""

import os

from shopsteward.adapters.vision.fake import FixtureCopyChecker, FixtureVisionAdapter
from shopsteward.adapters.vision.gemini import GeminiVisionAdapter
from shopsteward.adapters.vision.interface import CopyChecker, VisionAdapter
from shopsteward.adapters.vision.openrouter import OpenRouterVisionAdapter
from shopsteward.pipeline.config import COMMERCIAL_PROMPT_PATH, COPY_CHECK_PROMPT_PATH
from shopsteward.pipeline.models import TuningProfile


def build_copy_checker(profile: TuningProfile, live: bool) -> CopyChecker:
    """Fixture checker (never approves) unless live AND provider=openrouter
    (the only provider with a copy checker; anything else fails closed)."""
    if not live or profile.vision.provider != "openrouter":
        return FixtureCopyChecker()
    return OpenRouterVisionAdapter(
        api_key=os.environ["OPENROUTER_API_KEY"],
        prompt=COPY_CHECK_PROMPT_PATH.read_text(encoding="utf-8"),
        pricing=profile.vision.est_cost_per_mtok,
    )


def build_vision_adapter(profile: TuningProfile, live: bool) -> VisionAdapter:
    if not live:
        return FixtureVisionAdapter()

    # encoding="utf-8" explicit (M4, guardrail review 2026-08-25) -- this is
    # a PROMPT sent to an LLM; a silent cp1252 mojibake corruption on
    # Windows changes the prompt without raising.
    prompt = COMMERCIAL_PROMPT_PATH.read_text(encoding="utf-8")
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
