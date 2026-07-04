"""Pydantic v2 boundary models for the mockups module."""

from pydantic import BaseModel, ConfigDict, Field


class TemplateRegion(BaseModel):
    kind: str
    quad: list[list[float]]
    region_width_inches: float


class StagingTemplate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(alias="schema")
    template_id: str
    room_type: str
    style: str
    lighting: str
    orientation: str
    regions: list[TemplateRegion]
    tags: list[str] = Field(default_factory=list)


class TemplateReport(BaseModel):
    registered: int = 0
    updated: int = 0
    invalid: int = 0
    unchanged: int = 0


class MockupRecord(BaseModel):
    path: str
    photo_id: str | None = None
    landing_file_id: str
    set_key: str
    intent: str
    template_id: str | None = None
    params: dict = Field(default_factory=dict)


class MockupJobResult(BaseModel):
    sets_completed: int = 0
    mockups_written: int = 0
    skipped_idempotent: int = 0
    intents_skipped_no_template: int = 0
    templates_invalid: int = 0


class _IntentConfig(BaseModel):
    enabled: bool
    count: int


class _ShadowConfig(BaseModel):
    offset_frac: float
    blur_frac: float
    opacity: float
    angle_deg: float


class _LightMatchConfig(BaseModel):
    enabled: bool
    gain_min: float
    gain_max: float
    wb_min: float
    wb_max: float


class _RenderConfig(BaseModel):
    output_long_edge_px: int
    jpeg_quality: int
    mat_fraction: float
    mat_color: list[int]
    frame_width_inches: float
    frame_color: list[int]
    canvas_wrap_depth_inches: float
    shadow: _ShadowConfig
    light_match: _LightMatchConfig


class _ProductsConfig(BaseModel):
    default_print_widths_inches: dict[str, float]


class _WhatYouGotConfig(BaseModel):
    sizes: list[str]
    formats: list[str]
    headline: str


class _ListingCopyConfig(BaseModel):
    ai_disclosure_line: str


class MockupConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(alias="schema")
    intents: dict[str, _IntentConfig]
    render: _RenderConfig
    products: _ProductsConfig
    whatyougot: _WhatYouGotConfig
    listing_copy: _ListingCopyConfig
