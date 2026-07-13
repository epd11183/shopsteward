"""Pydantic v2 boundary models for the pipeline module."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

# VisionVerdict is owned by shopsteward.adapters.vision.interface (import-linter
# contracts make adapters -> pipeline imports forbidden, not the reverse).
# Re-exported here so existing `from shopsteward.pipeline.models import
# VisionVerdict` call sites keep working.
from shopsteward.adapters.vision.interface import VisionVerdict

__all__ = [
    "Gate1Card",
    "LandingConfig",
    "LandingReport",
    "ScoreBreakdown",
    "ScoringConfig",
    "ScoringRunResult",
    "TuningProfile",
    "VisionConfig",
    "VisionVerdict",
]


class ScoringConfig(BaseModel):
    weights: dict[str, float]
    gate1_threshold: int
    borderline_band: int
    hero_preset_family: str
    technical: dict[str, float]


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
    scoring: ScoringConfig
    vision: VisionConfig
    landing: LandingConfig


class ScoreBreakdown(BaseModel):
    technical: float | None = None
    commercial: float | None = None
    catalog_gap: float | None = None
    historical_conversion: float | None = None
    composite: float
    escalated: bool = False


class Gate1Card(BaseModel):
    photo_id: str
    base_name: str
    composite: float
    technical: float | None = None
    commercial: float | None = None
    subject: str
    strongest_room_style: str
    one_risk: str
    rationale: str
    escalated: bool = False
    state: str
    edit_job_id: str | None = None
    dispatch_state: str | None = None


class LandingReport(BaseModel):
    observed: int = 0
    matched: int = 0
    manual_drops: int = 0
    invalid: int = 0


class ScoringRunResult(BaseModel):
    scored: int = 0
    queued: int = 0
    escalated: int = 0
    failed: int = 0
    cap_hit: bool = False
