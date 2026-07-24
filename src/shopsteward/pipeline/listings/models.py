"""Pydantic v2 boundary models for the listings module.

Slice 1 (draft-build stage) exercises ListingConfig, ListingImage,
SellableFile, ListingDraft and BuildReport. Slice 2 (copy + pricing) adds
PricingRules and Economics here, and re-exports CopyInputs from
adapters.copy.interface (VisionVerdict precedent -- see that module's
docstring for why CopyInputs is owned by the adapter, not here). Slice 4
(Gate 3) adds Gate3Card (queue read model) and GateEditFields (partial-edit
input validation -- tag count/length rules mirror adapters.copy.interface's
CopyVerdict but stay separate since every field here is optional, a Gate 3
edit touches only the fields the operator actually changed).
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shopsteward.adapters.copy.interface import CopyInputs

__all__ = [
    "BuildReport",
    "CopyInputs",
    "Economics",
    "Gate3Card",
    "GateEditFields",
    "ListingConfig",
    "ListingDraft",
    "ListingImage",
    "PricingRules",
    "SellableFile",
]


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


class PricingRules(BaseModel):
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
    pricing: PricingRules
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


class Economics(BaseModel):
    price: float
    etsy_fees: float
    net: float


_MAX_TAGS = 13
_MAX_TAG_LEN = 20


class GateEditFields(BaseModel):
    """Partial Gate 3 edit -- every field is optional (decision 40: edits are
    never required); only fields present get validated, persisted, and sent
    to update_listing."""

    title: str | None = Field(default=None, min_length=1, max_length=140)
    tags: list[str] | None = Field(default=None, min_length=1, max_length=_MAX_TAGS)
    description: str | None = Field(default=None, min_length=1)
    price: float | None = None

    @field_validator("tags")
    @classmethod
    def _tags_within_length(cls, tags: list[str] | None) -> list[str] | None:
        if tags is None:
            return tags
        for tag in tags:
            if not tag.strip():
                raise ValueError("empty tag not allowed (Etsy rejects blank tags)")
            if len(tag) > _MAX_TAG_LEN:
                raise ValueError(f"tag {tag!r} exceeds {_MAX_TAG_LEN} chars")
        return tags


class Gate3Card(BaseModel):
    draft_id: str
    etsy_listing_id: str | None
    title: str | None
    tags: list[str]
    description: str | None
    price: float | None
    currency: str | None
    margin_floor: float | None
    economics: Economics | None
    images: list[ListingImage]
    file_source: str | None
    state: str
    retry_error: str | None = None
