"""Pydantic v2 boundary models for the listings module.

Slice 1 (draft-build stage) exercises ListingConfig, ListingImage,
SellableFile, ListingDraft and BuildReport. Slice 2 (copy + pricing) adds
PricingRules and Economics here, and re-exports CopyInputs from
adapters.copy.interface (VisionVerdict precedent -- see that module's
docstring for why CopyInputs is owned by the adapter, not here). Slice 4
(Gate 3) adds Gate3Card (queue read model) and GateEditFields (partial-edit
input validation -- every field here is optional, a Gate 3 edit touches
only the fields the operator actually changed). Per-tag content validation
(blank/length/comma) is shared with adapters.copy.interface's CopyVerdict
via adapters.copy.tags.validate_tag; the count/length ceilings
(MAX_TAGS/MAX_TAG_LEN) come from the same module.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shopsteward.adapters.copy.interface import CopyInputs
from shopsteward.adapters.copy.tags import MAX_TAG_LEN as _MAX_TAG_LEN
from shopsteward.adapters.copy.tags import MAX_TAGS as _MAX_TAGS
from shopsteward.adapters.copy.tags import validate_tag
from shopsteward.pipeline.models import LandingReport

__all__ = [
    "AdoptReport",
    "AssetStoreConfig",
    "BuildReport",
    "CopyInputs",
    "Economics",
    "Gate3Card",
    "GateEditFields",
    "ListingConfig",
    "ListingDraft",
    "ListingImage",
    "MatchConfig",
    "MatchResult",
    "PricingRules",
    "ResetPlanRow",
    "ResetReport",
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


class MatchConfig(BaseModel):
    """Perceptual-hash match thresholds for the `archive adopt-local`
    backfill (design: source-photo-match). Config-over-code (CLAUDE.md) --
    lives nested in AssetStoreConfig rather than a parallel config file
    since it only ever governs the same archive/adopt flow."""

    max_distance: int = 6
    min_margin: int = 4


class AssetStoreConfig(BaseModel):
    """Config for the managed local archive (source-asset head, design
    2026-08-11): where the untouched original master bytes are copied so a
    reprint can resolve them after the landing folder is cleared. `root` is
    resolved relative to the repo root by asset_store_config.resolve_root --
    tests override it to a tmp dir, NEVER the real `data/` (CLAUDE.md hard
    guardrail)."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(alias="schema")
    name: str
    root: str
    enabled: bool
    match: MatchConfig = Field(default_factory=MatchConfig)


class MatchResult(BaseModel):
    """One row of the `archive adopt-local` dry-run table: a live Etsy
    listing's classification against the local candidate files."""

    listing_id: int
    local_path: str | None
    distance: int | None
    verdict: str  # "match" | "ambiguous" | "unmatched" | "pinned"


class AdoptReport(BaseModel):
    matched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    pinned: int = 0
    adopted: int = 0
    revoked: int = 0


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
    # M5b addition (design §5): net / price, so a POD variant's margin
    # against its floor is visible without recomputing it at every call
    # site. Digital call sites get it for free too -- unit_cost defaults to
    # 0.0 there, so it's just net/price, harmless to display.
    margin_pct: float = 0.0


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
            validate_tag(tag, max_len=_MAX_TAG_LEN)
        return tags


class ResetPlanRow(BaseModel):
    """One row of `listings reset`'s dry-run plan (winners-batch reset,
    pipeline/listings/reset.py). `verdict` is one of: "reset" (free, no
    confirmation needed), "needs_confirmation" (pushed/POD-linked, requires
    --include-pushed + --confirm-listing-id), "refused_published", or
    "refused_adopted" -- the latter two are hard refusals with no override
    flag."""

    draft_id: str
    landing_file_id: str | None
    state: str
    format: str | None
    etsy_listing_id: str | None
    provider_product_id: str | None
    pod_status: str | None
    verdict: Literal["reset", "needs_confirmation", "refused_published", "refused_adopted"]


class ResetReport(BaseModel):
    """Result of `listings reset --apply` (pipeline/listings/reset.py's
    apply_reset). `landing` is None when --keep-landing was passed (no
    landing-row reset/re-observe happened at all)."""

    drafts_reset: int = 0
    landing_files_reset: int = 0
    landing: LandingReport | None = None


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
