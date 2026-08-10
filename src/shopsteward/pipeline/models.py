"""Pydantic v2 boundary models for the pipeline module."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

# VisionVerdict is owned by shopsteward.adapters.vision.interface (import-linter
# contracts make adapters -> pipeline imports forbidden, not the reverse).
# Re-exported here so existing `from shopsteward.pipeline.models import
# VisionVerdict` call sites keep working.
from shopsteward.adapters.vision.interface import VisionVerdict

__all__ = [
    "LandingConfig",
    "LandingReport",
    "TuningProfile",
    "VisionConfig",
    "VisionVerdict",
]


class VisionConfig(BaseModel):
    # Runtime AI calls route through OpenRouter by default (PRD §13 decision
    # 36); "gemini" selects the native GeminiVisionAdapter as a fallback.
    # New field with a default so profiles seeded before decision 36 (which
    # lack "provider" in their stored payload) still parse.
    provider: str = "openrouter"
    triage_model: str
    rescore_model: str
    max_long_edge_px: int
    est_cost_per_mtok: dict[str, dict[str, float]]
    monthly_soft_cap_usd: float

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        if v not in ("openrouter", "gemini"):
            raise ValueError(f"vision.provider must be 'openrouter' or 'gemini', got {v!r}")
        return v


class LandingConfig(BaseModel):
    min_long_edge_px: int
    allowed_formats: list[str]


class TuningProfile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(alias="schema")
    name: str
    vision: VisionConfig
    landing: LandingConfig


class LandingReport(BaseModel):
    observed: int = 0
    matched: int = 0
    manual_drops: int = 0
    invalid: int = 0
