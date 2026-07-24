"""Pydantic v2 boundary models for the listings module.

Slice 1 (draft-build stage) only exercises ListingConfig, ListingImage,
SellableFile, ListingDraft and BuildReport. CopyInputs/PricingRules/
Economics/Gate3Card belong to later M5a slices (copy, pricing, Gate 3) --
added when those slices land, not speculatively here.
"""

from pydantic import BaseModel, ConfigDict, Field


class _ListingConfigCopy(BaseModel):
    provider: str
    model: str
    ab_alternate: str
    temperature: float
    append_disclosure: bool
    prompt_path: str
    house_style_path: str
    est_cost_per_mtok: dict[str, dict[str, float]]


class _ListingConfigPricingFormat(BaseModel):
    base_price: float
    margin_floor: float


class _ListingConfigEtsyFees(BaseModel):
    listing_fee: float
    transaction_pct: float
    payment_pct: float
    payment_flat: float


class _ListingConfigPricing(BaseModel):
    currency: str
    digital_quantity: int
    formats: dict[str, _ListingConfigPricingFormat]
    etsy_fees: _ListingConfigEtsyFees


class _ListingConfigEtsy(BaseModel):
    who_made: str
    when_made: str
    is_supply: bool
    taxonomy_id: int
    should_auto_renew: bool
    sellable_max_bytes: int


class ListingConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(alias="schema")
    name: str
    # JSON key stays "copy" (design §6); attribute renamed so it doesn't
    # shadow BaseModel.copy().
    copy_: _ListingConfigCopy = Field(alias="copy")
    pricing: _ListingConfigPricing
    image_order: list[str]
    image_cap: int
    etsy: _ListingConfigEtsy


class ListingImage(BaseModel):
    path: str
    intent: str
    rank: int


class SellableFile(BaseModel):
    source: str  # "landing_original" | "derived_jpeg"
    sha256: str
    bytes: int


class ListingDraft(BaseModel):
    draft_id: str
    landing_file_id: str
    photo_id: str | None = None
    set_key: str
    provider: str = "etsy_digital"
    format: str = "digital_download"
    sku_source: str = "etsy"
    listing_type: str = "download"
    etsy_listing_id: str | None = None
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    price: float | None = None
    currency: str | None = None
    margin_floor: float | None = None
    images: list[ListingImage] = Field(default_factory=list)
    file_source: str | None = None
    state: str = "built"
    created_at: str | None = None
    published_at: str | None = None


class BuildReport(BaseModel):
    drafts_built: int = 0
    pushed: int = 0
    copy_calls: int = 0
    skipped_idempotent: int = 0
    push_failed: int = 0
