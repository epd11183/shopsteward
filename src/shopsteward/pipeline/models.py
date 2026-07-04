"""Pydantic v2 boundary models for the pipeline module."""

from pydantic import BaseModel, ConfigDict, Field


class ScoringConfig(BaseModel):
    weights: dict[str, float]
    gate1_threshold: int
    borderline_band: int
    hero_preset_family: str
    technical: dict[str, float]


class VisionConfig(BaseModel):
    triage_model: str
    rescore_model: str
    max_long_edge_px: int
    est_cost_per_mtok: dict[str, dict[str, float]]
    monthly_soft_cap_usd: float


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


class VisionVerdict(BaseModel):
    commercial_score: int = Field(ge=0, le=100)
    subject: str
    strongest_room_style: str
    one_risk: str
    rationale: str = Field(max_length=140)


class Gate1Card(BaseModel):
    photo_id: str
    composite: float
    scores: ScoreBreakdown
    subject: str
    strongest_room_style: str
    one_risk: str
    rationale: str
    escalated: bool = False
    dispatch_state: str | None = None


class LandingReport(BaseModel):
    observed: int = 0
    invalid: int = 0
    matched: int = 0
    unmatched: int = 0
