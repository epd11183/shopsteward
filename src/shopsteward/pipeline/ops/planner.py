"""LLM narration of the deterministic Brief (M8b slice 1, design §5/§6) and
the intent-proposing planner validation gate (M8b slice 2, design §2/§6 --
the safety-critical piece of this milestone).

narrate_brief() is the only place that decides whether a narration call is
allowed: it reuses the shared `llm_ledger` monthly soft cap (the same pool
copy/vision spend against, PRD §13 decisions 22/38) rather than a
planner-specific cap. Zero new actions -- this only narrates the
already-rendered deterministic brief text; it proposes nothing.

plan_proposals() is the validation gate: the LLM's `ProposalIntent`s are
NEVER trusted directly. Every intent is dropped unless it survives, in
order: (1) the `customer_contact_barred` denylist (Legal belt-and-suspenders
-- even a mis-registered capability can't reach a customer), (2) it names a
capability in the passed-in registry (`unknown_capability` -- the
finite-action-space guarantee), (3) that capability's `policy_verified`
(`policy_unverified`), (4) that capability's own `materialize()` re-deriving
a real `ProposedAction` from SQL for the named target (`ungrounded` --
catches a hallucinated/ineligible target, since materialize() shares
propose()'s own grounding function). Survivors are then capped per
capability (`per_run_cap`) in the order the LLM emitted them. Every drop is
recorded as a `planner.intent_dropped` event -- "why didn't Claude propose X"
is always answerable from the log (mirrors the governor's
refusal-is-an-event precedent). The runner is never touched here: this
function only ever returns a `list[ProposedAction]`, identical in shape to
what a capability's own `propose()` returns -- `govern()`/execution stay
entirely downstream, in `runner.run()`."""

import json
import logging
import sqlite3

import httpx

from shopsteward.adapters.planner.interface import (
    CapabilityDescriptor,
    PlannerAdapter,
    PlannerLimits,
    PlannerParseError,
)
from shopsteward.core.events import Event, append
from shopsteward.pipeline.llm_ledger import monthly_spend
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.capabilities.seo_edit import _latest_observed
from shopsteward.pipeline.ops.models import OpsConfig, ProposedAction
from shopsteward.pipeline.ops.registry import Capability

logger = logging.getLogger(__name__)

# Legal belt-and-suspenders (design §2 step 1/§10 Chief Legal improvement):
# any capability whose key even LOOKS like it addresses a customer is barred
# here too, even if it was somehow registered with policy_verified=True --
# provable from the planner.intent_dropped log, independent of the registry.
_CUSTOMER_CONTACT_TERMS = ("message", "reply", "review", "refund", "dispute", "convo")


def narrate_brief(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: PlannerAdapter,
    brief_text: str,
    *,
    soft_cap_usd: float,
    model: str,
) -> str | None:
    """Returns the narration text, or None when narration is skipped (over
    the shared monthly cap) or fails (transport/parse error) -- the caller
    always still has the deterministic brief text to print either way."""
    if monthly_spend(conn, user_id) >= soft_cap_usd:
        logger.warning(
            "monthly llm.call soft cap reached (>= %.2f usd); skipping brief narration",
            soft_cap_usd,
        )
        return None

    try:
        narration = adapter.narrate(brief_text)
    except (PlannerParseError, httpx.HTTPError) as exc:
        logger.warning("brief narration unavailable: %s", type(exc).__name__)
        return None

    append(
        conn,
        Event(
            user_id=user_id,
            type="llm.call",
            payload={
                "producer": "ops.planner.narrate",
                "model": model,
                "est_cost_usd": narration.usage.est_cost_usd,
                "prompt_tokens": narration.usage.prompt_tokens,
                "completion_tokens": narration.usage.completion_tokens,
            },
        ),
    )
    return narration.text


def _is_customer_contact_barred(capability_key: str) -> bool:
    lowered = capability_key.lower()
    return any(term in lowered for term in _CUSTOMER_CONTACT_TERMS)


def _expired_with_sales(conn: sqlite3.Connection, user_id: int, cfg: OpsConfig) -> list[dict]:
    """listing_id/title/lifetime_sales for every EXPIRED listing meeting
    `cfg.renew.min_lifetime_sales` real (non-fixture) sale line-items ever
    recorded (proj_sale_items -- see renew.py's own module docstring) AND
    `quantity >= 1` -- the same two-part bar both listing.seo_edit's
    expired-with-sales branch and listing.renew use, so the LLM is never
    handed a target either would silently drop as ineligible. `quantity`
    isn't in proj_listings, so it's checked via the same `_latest_observed`
    event-reconstruction seo_edit.py's own eligibility uses. materialize()'s
    own `_eligible()` still re-grounds and drops anything ineligible; this
    is a courtesy to the model, not the safety boundary (module docstring)."""
    rows = conn.execute(
        "SELECT pl.listing_id, pl.title, COUNT(si.transaction_id) AS lifetime_sales "
        "FROM proj_listings pl JOIN proj_sale_items si "
        "ON si.user_id=pl.user_id AND si.listing_id=pl.listing_id "
        "WHERE pl.user_id=? AND pl.state='expired' "
        "GROUP BY pl.listing_id, pl.title "
        "HAVING COUNT(si.transaction_id) >= ?",
        (user_id, cfg.renew.min_lifetime_sales),
    ).fetchall()
    out = []
    for r in rows:
        listing = _latest_observed(conn, user_id, r["listing_id"])
        if listing is None or listing.quantity < 1:
            continue
        out.append(
            {
                "listing_id": r["listing_id"],
                "title": r["title"],
                "lifetime_sales": r["lifetime_sales"],
            }
        )
    return out


def _build_facts_json(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, capabilities: list[Capability]
) -> str:
    """Real SQL only (design §2/§6): dead-listing candidates + trending from
    `analytics.py`, plus, per registered capability, the exact grounded
    target_ids its own `propose()` already computed -- the LLM chooses
    AMONG these, it never invents a target (`materialize()` re-checks
    regardless, so this is a courtesy to the model, not the safety
    boundary)."""
    dead = analytics.dead_listings(conn, user_id, cfg)
    trend = analytics.trending(conn, user_id, cfg)
    viewed_not_sold = analytics.viewed_not_sold(conn, user_id)
    sellers = analytics.top_sellers(conn, user_id, cfg)
    expired_with_sales = _expired_with_sales(conn, user_id, cfg)
    facts = {
        "dead_listings": [dl.model_dump(mode="json") for dl in dead],
        "trending": [t.model_dump(mode="json") for t in trend],
        # listing.seo_edit is planner-only (propose() always []) -- without
        # this block the LLM has no target ids for it at all (dead_listings/
        # trending are the only other real-data blocks here). Also the same
        # signal listing.reprice keys on. materialize() still re-grounds and
        # drops anything ineligible -- this is target discovery, not trust.
        "viewed_not_sold": [vns.model_dump(mode="json") for vns in viewed_not_sold],
        # listing.seo_edit's OTHER eligibility branch (expired + real
        # historical sales -- same real target ids listing.renew proposes
        # for reactivation). materialize()'s _eligible() still re-grounds.
        "expired_with_sales": expired_with_sales,
        # social.caption_draft is ALSO planner-only (propose() always []) --
        # without this block the LLM has no target ids for it either (M8b
        # slice 6). materialize() still re-grounds against the SAME
        # analytics.top_sellers() and drops anything ineligible.
        "top_sellers": [s.model_dump(mode="json") for s in sellers],
        "candidate_target_ids": {
            cap.key: [a.target_id for a in cap.propose(conn, user_id, cfg)] for cap in capabilities
        },
    }
    return json.dumps(facts, sort_keys=True)


def _drop(
    conn: sqlite3.Connection, user_id: int, reason: str, capability_key: str, target_id: str
) -> None:
    append(
        conn,
        Event(
            user_id=user_id,
            type="planner.intent_dropped",
            payload={"reason": reason, "capability_key": capability_key, "target_id": target_id},
        ),
    )


def plan_proposals(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    adapter: PlannerAdapter,
    capabilities: list[Capability],
    *,
    soft_cap_usd: float,
) -> list[ProposedAction]:
    """Returns the validated, materialized `ProposedAction`s the planner's
    intents survive the gate to become -- never anything the LLM invented
    directly. Over the shared monthly cap, or a transport/parse failure,
    returns `[]` so the caller falls back to the deterministic `propose()`
    path -- this function NEVER raises for a provider/cost problem."""
    if monthly_spend(conn, user_id) >= soft_cap_usd:
        logger.warning(
            "monthly llm.call soft cap reached (>= %.2f usd); skipping planner.plan()",
            soft_cap_usd,
        )
        return []

    catalog = [
        CapabilityDescriptor(
            key=cap.key, purpose=getattr(cap, "purpose", cap.key), max_tier=int(cap.max_tier)
        )
        for cap in capabilities
    ]
    facts_json = _build_facts_json(conn, user_id, cfg, capabilities)
    limits = PlannerLimits(
        reprice_min_price_usd=cfg.reprice.min_price_usd,
        reprice_max_pct_change=cfg.reprice.max_pct_change,
        seo_edit_min_lifetime_views=cfg.seo_edit.min_lifetime_views,
        caption_max_len=cfg.caption.max_len,
    )

    try:
        plan = adapter.plan(facts_json, catalog, limits)
    except (PlannerParseError, httpx.HTTPError) as exc:
        logger.warning("planner.plan() unavailable: %s", type(exc).__name__)
        return []

    append(
        conn,
        Event(
            user_id=user_id,
            type="llm.call",
            payload={
                "producer": "ops.planner.plan",
                "model": cfg.planner.model,
                "est_cost_usd": plan.usage.est_cost_usd,
                "prompt_tokens": plan.usage.prompt_tokens,
                "completion_tokens": plan.usage.completion_tokens,
            },
        ),
    )

    cap_by_key = {cap.key: cap for cap in capabilities}
    max_per_cap = cfg.autonomy.planner_max_per_capability_per_run
    kept_counts: dict[str, int] = {}
    proposals: list[ProposedAction] = []

    for intent in plan.intents:
        if _is_customer_contact_barred(intent.capability_key):
            _drop(conn, user_id, "customer_contact_barred", intent.capability_key, intent.target_id)
            continue

        cap = cap_by_key.get(intent.capability_key)
        if cap is None:
            _drop(conn, user_id, "unknown_capability", intent.capability_key, intent.target_id)
            continue

        if not cap.policy_verified:
            _drop(conn, user_id, "policy_unverified", intent.capability_key, intent.target_id)
            continue

        action = cap.materialize(conn, user_id, cfg, intent)
        if action is None:
            _drop(conn, user_id, "ungrounded", intent.capability_key, intent.target_id)
            continue

        if kept_counts.get(intent.capability_key, 0) >= max_per_cap:
            _drop(conn, user_id, "per_run_cap", intent.capability_key, intent.target_id)
            continue

        kept_counts[intent.capability_key] = kept_counts.get(intent.capability_key, 0) + 1
        proposals.append(action)

    return proposals
