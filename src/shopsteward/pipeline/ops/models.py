"""Pydantic v2 boundary models for M8a slice 1: ops.json config (schema
shopsteward.ops/1, ListingConfig/PodConfig alias convention) and the pure
analytics/brief output shapes (design §6/§7/§9 slice 1).

No autonomy-chassis models here (Tier, ProposedAction, CapabilitySpec,
Brief section toggles for NEEDS YOU/DONE OVERNIGHT/REFUSED/AUTONOMY) --
those need the registry/governor/runner from slices 2+ and are explicitly
out of scope for slice 1 (design §9, §7's exclusion list)."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Brief",
    "DeadListing",
    "ListingSales",
    "OpsConfig",
    "ProductTypeStat",
    "RevenueWindow",
    "ShootMoreSuggestion",
    "SizeStat",
    "TrendingListing",
    "ViewedNotSold",
]


class _OpsWindows(BaseModel):
    revenue_window_days: int = Field(gt=0)
    trend_window_days: int = Field(gt=0)


class _OpsDeadListing(BaseModel):
    window_days: int = Field(gt=0)
    min_observed_days: int = Field(gt=0)


class _OpsShootMore(BaseModel):
    max_listing_count: int = Field(gt=0)


class _OpsBriefSections(BaseModel):
    revenue: bool = True
    selling: bool = True
    dying: bool = True
    shoot_more: bool = True
    data_quality: bool = True


class OpsConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(alias="schema")
    name: str
    windows: _OpsWindows
    dead_listing: _OpsDeadListing
    shoot_more: _OpsShootMore
    # product_type -> list of case-insensitive title substrings that
    # classify a listing into that type. Config-driven per CLAUDE.md
    # "configuration over code" -- never hardcoded in analytics.py. No
    # substring match => product_type "unknown" (never guessed).
    product_type_keywords: dict[str, list[str]]
    brief_sections: _OpsBriefSections


# --- analytics/brief output (pure, no LLM, no network) ----------------------


class RevenueWindow(BaseModel):
    window_days: int
    start: date
    end: date
    current_usd: float
    prior_usd: float
    pct_change: float | None  # None when prior window had $0 (undefined growth rate)
    orders: int
    units: int


class ListingSales(BaseModel):
    listing_id: int
    title: str
    units: int
    revenue_usd: float


class ViewedNotSold(BaseModel):
    listing_id: int
    title: str
    views_lifetime: int


class DeadListing(BaseModel):
    listing_id: int
    title: str
    days_observed: int
    views_in_window: int


class TrendingListing(BaseModel):
    listing_id: int
    title: str
    views_recent: int
    views_prior: int


class ProductTypeStat(BaseModel):
    product_type: str
    listing_count: int  # active catalog count of this type, not just ones that sold
    units: int
    revenue_usd: float


class SizeStat(BaseModel):
    size: str | None  # None = no size signal extracted from the title
    units: int
    revenue_usd: float


class ShootMoreSuggestion(BaseModel):
    product_type: str
    listing_count: int
    revenue_usd: float


class Brief(BaseModel):
    generated_at: date
    window_days: int
    revenue: RevenueWindow | None = None
    top_sellers: list[ListingSales] = Field(default_factory=list)
    viewed_not_sold: list[ViewedNotSold] = Field(default_factory=list)
    dead_listings: list[DeadListing] = Field(default_factory=list)
    trending: list[TrendingListing] = Field(default_factory=list)
    product_type_breakdown: list[ProductTypeStat] = Field(default_factory=list)
    size_breakdown: list[SizeStat] = Field(default_factory=list)
    shoot_more: list[ShootMoreSuggestion] = Field(default_factory=list)
    data_quality_notes: list[str] = Field(default_factory=list)
