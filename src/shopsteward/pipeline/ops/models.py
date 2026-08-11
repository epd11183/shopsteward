"""Pydantic v2 boundary models for M8a slice 1 (ops.json config, analytics/
brief output shapes -- design §6/§7/§9 slice 1) plus the autonomy-chassis
models added in PR1 of the chassis slice (M8a spec §3/§8.1): Tier,
ProposedAction, ExecutionResult, RefusalReason, CapabilityState. The
Capability Protocol itself lives in registry.py, not here (it needs
sqlite3.Connection in its method signatures, which models.py -- a pure
boundary-shape module -- has no other reason to import)."""

from datetime import date
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Brief",
    "BriefAction",
    "BriefAutonomy",
    "BriefLadderRow",
    "BriefProposal",
    "BriefRefusal",
    "CapabilityState",
    "DeadListing",
    "ExecutionResult",
    "ListingSales",
    "OpsConfig",
    "ProductTypeStat",
    "ProposedAction",
    "RefusalReason",
    "RevenueWindow",
    "ShootMoreSuggestion",
    "SizeStat",
    "Tier",
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


class _OpsPlanner(BaseModel):
    # LLM narration of the deterministic Brief (M8b slice 1, design §5) --
    # OpenRouter-only (PRD §13 decision 36). No spend cap here: narration
    # reuses the shared llm_ledger monthly soft cap (tuning
    # vision.monthly_soft_cap_usd), see pipeline/ops/planner.py.
    model: str
    est_cost_per_mtok: dict[str, dict[str, float]]


class _OpsBriefSections(BaseModel):
    revenue: bool = True
    selling: bool = True
    dying: bool = True
    shoot_more: bool = True
    data_quality: bool = True
    autonomy: bool = True


class _OpsLadder(BaseModel):
    promote_approvals: int = Field(gt=0)
    promote_min_days: int = Field(gt=0)
    t1_executions: int = Field(gt=0)
    t1_min_days: int = Field(gt=0)


class _OpsReprice(BaseModel):
    # `listing.reprice` (M8b slice 3, draft #9/#9b) thresholds. min_price_usd
    # is the absolute floor propose()/materialize() will never go below;
    # max_pct_change bounds how far even an LLM-proposed price may move from
    # the current price in either direction; default_reduction_pct is
    # propose()'s own deterministic default (a price DECREASE -- there is no
    # demand model yet, so this only ever proposes trying a lower price on a
    # viewed-but-not-selling listing, never a raise); min_lifetime_views is
    # the "enough traffic to judge this overpriced, not just new" floor.
    min_price_usd: float = Field(gt=0)
    max_pct_change: float = Field(gt=0, lt=1)
    default_reduction_pct: float = Field(gt=0, lt=1)
    min_lifetime_views: int = Field(gt=0)


class _OpsSeoEdit(BaseModel):
    # `listing.seo_edit` (M8b slice 4a, draft #10) eligibility threshold --
    # same "enough traffic to judge this, not just new" floor reprice uses,
    # kept as its own knob since the two capabilities' signal thresholds
    # aren't required to track each other.
    min_lifetime_views: int = Field(gt=0)


class _OpsAutonomy(BaseModel):
    # Chassis master switch + caps (M8a spec §3, draft §5). enabled and
    # monthly_spend_cap_usd MUST default false/0.00 -- nothing auto-executes,
    # nothing spends, until the operator opts in in config.
    enabled: bool = False
    daily_action_cap: int = Field(gt=0)
    per_capability_daily_cap: int = Field(gt=0)
    weekly_catalog_pct_cap: float = Field(gt=0, le=1)
    monthly_spend_cap_usd: float = Field(ge=0)
    proposal_ttl_days: int = Field(gt=0)
    ladder: _OpsLadder
    # LLM planner (M8b slice 2, design §7): default-off master switch --
    # False -> `ops run` uses today's deterministic propose() path,
    # unchanged. Also gated at runtime on live_planner_open() (flag+env+key).
    planner_enabled: bool = False
    # Per-capability cap on materialized proposals per `plan_proposals()` run
    # (design §10 CPO improvement) -- keeps NEEDS YOU short and high-signal.
    planner_max_per_capability_per_run: int = Field(gt=0, default=1)


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
    planner: _OpsPlanner
    brief_sections: _OpsBriefSections
    autonomy: _OpsAutonomy
    reprice: _OpsReprice
    seo_edit: _OpsSeoEdit


# --- autonomy chassis (PR1) --------------------------------------------------


class Tier(IntEnum):
    """T0..T3 == 0..3. LOWER number is MORE autonomous (draft §2.1/§2.4):
    promotion moves the number down (PROPOSE=2 -> NOTIFY=1 -> AUTO=0)."""

    AUTO = 0
    NOTIFY = 1
    PROPOSE = 2
    OPERATOR = 3


class ProposedAction(BaseModel):
    action_id: str  # sha256(capability|target_id|inputs_hash|ops_config_hash|day)
    capability: str
    target_type: str
    target_id: str  # str for uniformity (e.g. listing_id as str)
    tier: Tier  # effective tier at proposal time
    reason: str  # one human sentence (draft §2.3 invariant 4)
    inputs_hash: str
    estimated_cost_usd: float = 0.0
    undo_available: bool
    expires_at: str  # ISO date (proposed day + proposal_ttl_days)
    # A capability's own concrete decision (M8b slice 3, `listing.reprice`'s
    # target price) -- carried on the action itself so execute() applies
    # EXACTLY what was proposed/approved, never a recompute at execute time.
    # Additive/default-empty: autorenew/tune_threshold never set this, so
    # their action_id/inputs_hash are unchanged. Round-trips automatically
    # through the action.proposed event (model_dump()/model_validate()).
    # list[str] added (M8b slice 4a, `listing.seo_edit`'s tags).
    params: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    before: dict
    after: dict
    cost_usd: float = 0.0
    duration_ms: int = 0


class RefusalReason(StrEnum):
    """Exact wire strings (draft §5); governor precedence order matches
    this declaration order top-to-bottom."""

    HALTED = "halted"
    EXPIRED = "expired"
    POLICY_UNVERIFIED = "policy_unverified"
    PRECONDITION = "precondition"
    BUDGET = "budget"
    DAILY_CAP = "daily_cap"
    PER_CAPABILITY_CAP = "per_capability_cap"
    PORTFOLIO_CAP = "portfolio_cap"


class CapabilityState(BaseModel):
    """The promotion-ladder state for one capability (folds into
    proj_capability_state). Never constructed by hand outside
    projections.capability_states()/tiers.py tests -- it is derived from
    the event log, never written directly."""

    capability: str
    tier: Tier = Tier.PROPOSE
    approvals: int = 0
    rejections: int = 0
    undos: int = 0
    executions: int = 0
    tier_since: str  # ISO date
    last_action_at: str | None = None


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


# --- operator surface (PR3, M8a spec §8 PR3 / draft §6) ---------------------
# Deterministic reads over proj_actions/proj_capability_state -- no LLM, no
# network. Every field the operator needs to copy an action_id into
# `ops approve`/`ops reject`/`ops undo` lives on these models.


class BriefProposal(BaseModel):
    """One OPEN action.proposed the operator has not yet resolved."""

    action_id: str
    capability: str
    target_type: str
    target_id: str
    tier: Tier
    reason: str
    expires_at: str  # ISO date


class BriefAction(BaseModel):
    """One action.executed in the recent window, not since undone."""

    action_id: str
    capability: str
    target_id: str
    reason: str
    tier: Tier
    undo_available: bool


class BriefRefusal(BaseModel):
    """One action.refused in the recent window -- reason is the governor's
    RefusalReason wire string (e.g. "daily_cap"), not the proposal's own
    business reason."""

    capability: str
    target_id: str
    reason: str


class BriefLadderRow(BaseModel):
    capability: str
    tier: Tier
    approvals: int
    rejections: int
    executions: int
    undos: int
    tier_since: str  # ISO date


class BriefAutonomy(BaseModel):
    enabled: bool
    halted: bool
    month_spend_usd: float
    monthly_spend_cap_usd: float
    ladder: list[BriefLadderRow] = Field(default_factory=list)


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
    # PR3 chassis sections -- default-empty so slice-1 Brief construction
    # (generate_brief() with brief_sections.autonomy=False, or any caller
    # that never touches these) stays valid without passing them.
    needs_you: list[BriefProposal] = Field(default_factory=list)
    done_recent: list[BriefAction] = Field(default_factory=list)
    refused_recent: list[BriefRefusal] = Field(default_factory=list)
    autonomy: BriefAutonomy | None = None
