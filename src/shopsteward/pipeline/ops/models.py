"""Pydantic v2 boundary models for M8a slice 1 (ops.json config, analytics/
brief output shapes -- design §6/§7/§9 slice 1) plus the autonomy-chassis
models added in PR1 of the chassis slice (M8a spec §3/§8.1): Tier,
ProposedAction, ExecutionResult, RefusalReason, CapabilityState. The
Capability Protocol itself lives in registry.py, not here (it needs
sqlite3.Connection in its method signatures, which models.py -- a pure
boundary-shape module -- has no other reason to import)."""

from datetime import date
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Brief",
    "BriefAction",
    "BriefAutonomy",
    "BriefCaption",
    "BriefLadderRow",
    "BriefPin",
    "BriefProposal",
    "BriefRefusal",
    "CapabilityState",
    "DeadListing",
    "ExecutionResult",
    "ListingSales",
    "OpsConfig",
    "PinExperimentResult",
    "ProductTypeStat",
    "ProposedAction",
    "RefusalReason",
    "RevenueWindow",
    "SeoEditViewDelta",
    "ShootMoreSuggestion",
    "SizeStat",
    "StaleDraft",
    "Tier",
    "TrendingListing",
    "ViewedNotSold",
]


class _OpsWindows(BaseModel):
    revenue_window_days: int = Field(gt=0)
    trend_window_days: int = Field(gt=0)
    # T12 (operator-approved 2026-08-25 /autoplan gate: "Phase-2 trigger --
    # widened to trailing-90-day or lifetime sale plus a views-velocity
    # alternative"). Capability-GATING eligibility only
    # (analytics.proven_listings) -- NEVER used by top_sellers(), which stays
    # on revenue_window_days for the brief's "what's selling" section.
    proven_window_days: int = Field(gt=0, default=90)
    proven_min_lifetime_sales: int = Field(gt=0, default=1)
    # views-velocity alternative arm (zero-sales listings only): a listing
    # is also proven if its views grew by at least views_velocity_min_delta
    # over views_velocity_window_days. 30d, not revenue_window_days's 7d --
    # this shop's best listing has 87 LIFETIME views, so a 7d window is
    # almost always unmeasurable (< 2 observations); min_delta=5 is a small
    # bar sized to that same low-traffic reality.
    views_velocity_window_days: int = Field(gt=0, default=30)
    views_velocity_min_delta: int = Field(gt=0, default=5)


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
    captions: bool = True
    pins: bool = True
    pin_experiments: bool = True


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
    # Third eligibility branch (operator report, 2026-08): an active listing
    # with this few tags is search-invisible by construction -- flagged
    # regardless of views/sale-window, since "not enough traffic to judge
    # yet" doesn't apply to a listing Etsy can never surface at all. `ge=0`,
    # not `gt=0` like the sibling knobs above -- 0 is the operator's exact
    # ask ("zero tags") and IS the sensible default; configurable only in
    # case the operator later wants a slightly less strict bar.
    min_tags_before_flagged: int = Field(ge=0, default=0)
    # T6 (2026-08-25 guardrail review): per-listing cooldown -- governed as a
    # RefusalReason.INELIGIBLE (governor.py), NOT re-checked inside
    # `_eligible()`/execute(), same H1/H2a precedent every other rate/policy
    # condition in this chassis follows. Default 60 days: `min_lifetime_views`
    # was just lowered 25 -> 5, which makes this capability fire on much
    # thinner signal than before -- 60 days is roughly the same order as the
    # `renew`/`catalog_expand` weekly-pace knobs scaled up (~8 weeks), long
    # enough that a repeat edit only fires after Etsy's own search index has
    # had real time to reflect the previous one, short enough that a
    # genuinely still-underperforming listing isn't locked out for a season.
    cooldown_days: int = Field(gt=0, default=60)


class _OpsRenew(BaseModel):
    # `listing.renew` (M8b slice 4c) -- min_lifetime_sales is the "has this
    # ever actually sold" floor (a listing that never sold isn't worth
    # spending $0.20 to bring back); listing_fee_usd is Etsy's real renewal
    # charge, threaded through as estimated_cost_usd/ExecutionResult.cost_usd
    # so the governor's monthly spend cap actually sees the spend.
    min_lifetime_sales: int = Field(gt=0)
    # gt=0, not ge=0 (matches _OpsReprice.min_price_usd) -- zero is never a
    # legitimate value: Etsy always charges something on renewal, and a
    # zero-cost config would let the governor's month_spend() silently
    # undercount real spend while Etsy still charges the operator for it.
    listing_fee_usd: float = Field(gt=0)


class _OpsCatalogExpansion(BaseModel):
    # `listing.catalog_expand` (T11, 2026-08-25 design doc) -- paced digital
    # listings built from an operator-curated archive folder (source_folder);
    # putting a file there IS the "this is sellable" judgment (design §6.1).
    # min_long_edge_px defaults higher than the landing floor
    # (tuning_profile.json's 3000) -- see the design doc §6.2: whatyougot
    # copy advertises 16x20 at 300 DPI, which needs 6000px, and listing a
    # smaller file against that copy is an inaccurate listing (E16 condition
    # 2). listing_fee_usd uses gt=0 for the same reason _OpsRenew does -- a
    # zero would let month_spend() silently undercount real Etsy spend.
    enabled: bool = True
    source_folder: str
    recursive: bool = True
    max_new_per_week: int = Field(gt=0)
    min_long_edge_px: int = Field(gt=0)
    dedup_max_distance: int = Field(ge=0)
    listing_fee_usd: float = Field(gt=0)


class _OpsSocialChannel(BaseModel):
    """One `social.caption_draft` posting channel (T5+E5, 2026-08-25
    owned-channel premise-gate: /autoplan Decision Audit Trail #9 --
    "per-channel eligibility policy config + caption mark-posted + channel
    in target identity", explicitly REJECTING "copy-paste explore policy
    into caption_draft"). `eligibility` is `"explore"` (coverage-first,
    cooldown-gated, `social.pinterest_post`'s own policy -- correct for a
    free, individually-deletable, long-lived search-index entry, design
    doc §2.1) or `"proven"` (proof-first, gated on
    `analytics.proven_listings()` -- correct for a one-shot feed post that
    spends the shop's audience attention once and dies in a day,
    `social.caption_draft`'s ORIGINAL design). See
    `capabilities/caption_draft.py`'s module docstring for which policy
    IG/FB get by default and the argument for it -- the point of this
    field existing is that the choice is declared in config, with its own
    rationale, per channel, not silently hardcoded into one capability."""

    eligibility: Literal["explore", "proven"] = "proven"
    cooldown_days: int = Field(gt=0)


class _OpsCaption(BaseModel):
    # `social.caption_draft` (M8b slice 6, draft §3.3 #26) -- Instagram's
    # real caption character limit (config-over-code, not a tuning knob).
    max_len: int = Field(gt=0)
    # channel_key ("instagram", "facebook", ...) -> its own eligibility
    # policy + cooldown (T5+E5 above). REQUIRED (H1, guardrail review
    # 2026-08-25) -- same precedent as the `autonomy` block: a config seeded
    # before this field existed must NOT silently validate into a config
    # with zero channels (which makes `social.caption_draft` a silent no-op
    # on the LIVE shop -- `_candidates()` iterates an empty map, every
    # planner intent gets dropped as `hallucinated_target`, and nothing
    # says why). A required field instead routes a pre-T5 stored config
    # through `config.apply()`'s EXISTING schema-drift auto-repair path
    # (config.py's own docstring: a stored config that no longer validates
    # is treated as changed and replaced from config/defaults/ops.json) --
    # the same mechanism that already exists for exactly this situation,
    # rather than a second, bespoke "empty channels" escape hatch.
    channels: dict[str, _OpsSocialChannel]


class _OpsPinterest(BaseModel):
    # `social.pinterest_post` Variant A (`social.pin_drafted`, 2026-08-24
    # design doc §2/§3) -- cooldown_days is the anti-spam control replacing
    # a sales gate (design §2.1: a pin is cheap, deletable, and long-lived,
    # so eligibility is coverage-first, not proof-first). max_*_len are
    # PRODUCT limits kept within Pinterest's real ceilings (title<=100,
    # description<=800, alt_text<=500) -- not values copied 1:1 from
    # Pinterest's API spec that could be wrong. `boards` is a config-
    # declared board_key -> board name map so the LLM can never invent a
    # board (design §2.2).
    cooldown_days: int = Field(gt=0)
    max_title_len: int = Field(gt=0)
    max_description_len: int = Field(gt=0)
    max_alt_text_len: int = Field(gt=0)
    boards: dict[str, str] = Field(default_factory=dict)
    # E3 -- the pin-experiment holdout: governor.govern() refuses to
    # approve `social.pinterest_post` for a listing target if `listing.
    # seo_edit`/`listing.renew` executed for that SAME target within this
    # many days (and, symmetrically, refuses seo_edit/renew for a target
    # pinned within the window) -- confounding the P1 pin-experiment readout
    # otherwise (governor.py's own docstring/`_holdout_blocked` for the
    # documented same-day priority carve-out).
    holdout_days: int = Field(gt=0, default=7)


class _OpsSocial(BaseModel):
    """E9 (2026-08-25): cross-channel draft-staleness surfacing
    (`analytics.stale_drafts()`) -- a brief/analytics READ, never a written
    event (module docstring precedent: read-time computation against an
    injected `as_of`, replay-determinism-safe). `staleness_days` matches the
    longer of the two shipped caption channel cooldowns (14d default,
    `_OpsSocialChannel.cooldown_days`) -- a draft that has outlived even its
    own cooldown without a `*_posted` event is unambiguously "aging
    unposted", not just "still inside its normal review window"."""

    staleness_days: int = Field(gt=0, default=14)


class _OpsKeywordProbe(BaseModel):
    """`keyword_probe.py` (2026-08-25) -- the free first-party Etsy
    keyword/competition research signal (findAllListingsActive, x-api-key
    only, no scope). Read-only market research, NOT an autonomy-chassis
    capability -- no Tier, no governor, nothing here spends or writes to
    Etsy (see keyword_probe.py's module docstring). `top_n` bounds how many
    of Etsy's own top-ranked (sort_on="score") results are pulled per probe
    for tag/price/favorites aggregation; `max_phrases_per_run` bounds a
    single `ops probe-keyword` invocation's phrase count (keeps a manual
    research session from hammering Etsy's per-second rate limit).
    `cadence_days` is informational only -- no scheduler reads it yet; it
    documents the intended re-probe cadence for when one is built, so probes
    accumulate into a meaningful time series rather than firing arbitrarily
    often."""

    top_n: int = Field(gt=0, default=25)
    max_phrases_per_run: int = Field(gt=0, default=5)
    cadence_days: int = Field(gt=0, default=14)


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
    renew: _OpsRenew
    catalog_expansion: _OpsCatalogExpansion
    caption: _OpsCaption
    pinterest: _OpsPinterest
    social: _OpsSocial = Field(default_factory=_OpsSocial)
    # Additive/default-factory (same precedent as `social` above) -- a
    # stored config seeded before this field existed must still validate.
    keyword_probe: _OpsKeywordProbe = Field(default_factory=_OpsKeywordProbe)


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
    HOLDOUT = "holdout"
    BUDGET = "budget"
    DAILY_CAP = "daily_cap"
    PER_CAPABILITY_CAP = "per_capability_cap"
    # H1 (guardrail review, 2026-08-25): a capability-specific weekly-pace
    # limit (currently only `listing.catalog_expand`'s
    # `cfg.catalog_expansion.max_new_per_week`) MUST be a governor refusal,
    # never an execute()-time ValueError -- see governor.py's own docstring.
    # A refusal leaves the action pending/approvable later; a raised
    # ValueError is caught by runner._execute_and_record as a TERMINAL
    # action.failed, permanently burning that action_id.
    PACE = "pace"
    # H2a (guardrail review, 2026-08-25, the SAME failure class as PACE
    # above, applied to `social.caption_draft`): a per-target RATE/POLICY
    # condition -- cooldown still active, or the channel's eligibility mode
    # (explore/proven) no longer clears for this listing -- is a governor
    # refusal, never an execute()-time raise. Distinct from PACE (an
    # aggregate weekly COUNT cap) and from PRECONDITION (a static,
    # capability-level flag set once at construction, e.g.
    # `listing.catalog_expand`'s `live_copy`) -- this is a per-action,
    # per-target eligibility check re-derived from current config/state.
    INELIGIBLE = "ineligible"
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


class PinExperimentResult(BaseModel):
    """One drafted pin's before/after views-per-day reading (P1, 2026-08-24
    design doc §3) -- **correlational, not attribution**: a view-count
    change around `drafted_at` could be driven by Etsy search, seasonality,
    another capability's action, or nothing at all, never provably the pin
    itself. `baseline_views_per_day`/`observed_views_per_day` are None, not
    0, whenever there isn't enough elapsed time or listing history to
    measure (`analytics._views_delta`'s absent-is-not-zero rule)."""

    listing_id: int
    action_id: str
    title: str
    drafted_at: str  # ISO date
    days_since_posted: int
    baseline_views_per_day: float | None
    observed_views_per_day: float | None
    delta_views_per_day: float | None  # None unless both sides are measurable


class SeoEditViewDelta(BaseModel):
    """T6 (2026-08-25): one executed `listing.seo_edit`'s before/after
    views-per-day reading, same correlational-only convention and None-not-0
    absence rule as PinExperimentResult above -- reuses `proj_listing_daily`
    (the "before" the SEO edit needs is already there, module docstring: no
    separate view-count capture at edit time, just a join on the edit's own
    `action.executed` timestamp)."""

    listing_id: int
    action_id: str
    title: str
    edited_at: str  # ISO date
    days_since_edit: int
    baseline_views_per_day: float | None
    observed_views_per_day: float | None
    delta_views_per_day: float | None  # None unless both sides are measurable


class StaleDraft(BaseModel):
    """E9 (2026-08-25): one drafted pin/caption older than
    `cfg.social.staleness_days` with no corresponding `*_posted` event yet --
    a read-time-only computation (`analytics.stale_drafts()`), never a
    written event. `channel` is `"pin"` for a Pinterest draft, or the
    caption's own configured channel name (e.g. `"instagram"`)."""

    channel: str
    listing_id: int
    action_id: str | None
    drafted_at: str  # ISO datetime
    days_stale: int


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


class BriefCaption(BaseModel):
    """One recent `social.caption_drafted` -- the operator copy-pastes
    `caption` to the named `channel` and posts manually (M8b slice 6; T5+E5
    2026-08-25 added `channel`/`action_id`, mirroring BriefPin's own
    mark-posted queue below). No publish, ever."""

    listing_id: int
    channel: str
    title: str
    caption: str
    drafted_at: str  # ISO datetime, from the event payload
    # The resolved action_id for `ops mark-posted` -- None only for a
    # pre-T5 legacy event that never carried one (BriefPin's own precedent).
    action_id: str | None = None


class BriefPin(BaseModel):
    """One recent `social.pin_drafted` (Variant A, draft-only) -- the
    operator copy-pastes it into Pinterest by hand, then runs
    `ops mark-posted <action_id>` to drop it from this queue. No Pinterest
    call, ever, mirroring BriefCaption above."""

    listing_id: int
    title: str
    description: str
    alt_text: str
    board_key: str
    destination_url: str
    image_url: str
    drafted_at: str  # ISO datetime, from the event payload
    # The resolved action_id for `ops mark-posted` -- None if it couldn't be
    # resolved (malformed/legacy destination_url); such a row is never
    # dropped by the posted-filter since it can't be matched either way.
    action_id: str | None = None


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
    # `social.caption_draft` (M8b slice 6) -- default-empty so slice-1 Brief
    # construction (or any caller that never touches this) stays valid
    # without passing it. Gated on brief_sections.captions.
    caption_drafts: list[BriefCaption] = Field(default_factory=list)
    # `social.pinterest_post` Variant A (2026-08-24 design doc §2/§3) --
    # default-empty, same reasoning as caption_drafts above.
    pin_drafts: list[BriefPin] = Field(default_factory=list)
    # P1 outcome readout (2026-08-24 design doc §3) -- default-empty, same
    # reasoning as pin_drafts above. Gated on brief_sections.pin_experiments.
    pin_experiments: list[PinExperimentResult] = Field(default_factory=list)
