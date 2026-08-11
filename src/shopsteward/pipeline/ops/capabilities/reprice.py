"""`listing.reprice` -- the first money-moving, parameterized autonomy
capability (M8b slice 3, design §4/§8 slice 3). Policy: Etsy E2 PERMITTED
via `updateListingInventory`/`listings_w` (2026-08-11 autonomy platform
policy). Draft #9: a price change is a **T2 ceiling, NEVER higher** --
`max_tier = Tier.PROPOSE`, and it can never be promoted regardless of the
ladder (runner._maybe_promote never raises a capability's effective tier
past its own Python `max_tier`; see registry.py's invariant 2 for why this
is a Python attribute, not config). Draft #9b / PRD decision 43: **POD
listing prices are FORBIDDEN** -- touching updateListingInventory on a
Printful/Gelato-backed listing rewrites provider SKUs -- so this capability
is **DIGITAL LISTINGS ONLY**. `_eligible()` is the ONE grounding function
shared by propose() and materialize() (M8b slice-2 planner-safety contract):
a listing that isn't `state=="active"` is never a candidate, and the
digital-only line itself is TWO independent guards, both required, checked
in every path (propose/materialize/execute) -- misclassifying a POD listing
as digital would silently corrupt a Gelato/Printful provider SKU, so this
does not rely on title-keyword matching alone:
  1. **Authoritative provider signal**: any listing_id that ever appears in
     a `listingdraft.provider_linked` event (pod/provider.py's Gelato/
     Printful create->poll->link flow -- read directly off the event log by
     type string, no import of pipeline.listings.pod, keeping the
     editing/pipeline import-boundary lint clean) is POD-backed and
     excluded, full stop, regardless of its title.
  2. **Conservative title classification**: for everything else, a title
     must match a `product_type_keywords["digital"]` substring AND match NO
     OTHER product type's keywords (canvas/acrylic/poster/...) --
     `_is_conservatively_digital()`, deliberately stricter than
     `analytics._classify_product_type` (left unchanged for its other
     callers). A title like "Instant Download Ready Canvas Print" matches
     "digital" but also "canvas" -- excluded, never guessed.
Both guards are re-checked at execute() time too, and reprice is T2 (the
operator approves every single reprice) as a final backstop either way.

Draft §4: price changes lack a demand model until M7, so this capability
proposes a single deterministic default (a price DECREASE on a
viewed-but-not-selling listing -- never a raise) and otherwise trusts the
LLM's own `params["price_usd"]`, but ONLY after validating it against the
same SQL-derived bounds a human operator would apply (>= min_price_usd,
within +/-max_pct_change of the current price, != current). An
out-of-bounds/non-numeric price is DROPPED (materialize() returns None,
never clamped or silently substituted) -- the LLM proposes, SQL disposes.

Holds its own EtsyWriteAdapter, injected at construction (the chassis
contract -- autorenew.py precedent). This module never imports or
constructs an adapter itself."""

import hashlib
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops.config import get_ops_config, ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id


@dataclass(frozen=True)
class _Target:
    listing_id: int
    current_price_usd: float
    lifetime_views: int


def _pod_linked_listing_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    """The authoritative POD signal (S1 fix): every listing_id ever reported
    `listingdraft.provider_linked` (pod/provider.py's Gelato/Printful
    create->poll->link flow) is provider-backed and must never be repriced,
    regardless of its title. Read by event-type string only -- this module
    never imports `pipeline.listings.pod`."""
    return {
        int(e.payload["etsy_listing_id"])
        for e in read_all(conn, "listingdraft.provider_linked")
        if e.user_id == user_id and e.payload.get("etsy_listing_id") is not None
    }


def _is_conservatively_digital(title: str, keywords: dict[str, list[str]]) -> bool:
    """Stricter than `analytics._classify_product_type` (left unchanged --
    other callers rely on its first-match behavior): true only if `title`
    matches a "digital" keyword AND matches NO other product type's
    keywords. A title matching both ("Instant Download Ready Canvas Print")
    is excluded, never guessed -- draft #9b: misclassifying a POD listing as
    digital corrupts a provider SKU."""
    lowered = title.lower()
    if not any(sub.lower() in lowered for sub in keywords.get("digital", [])):
        return False
    return not any(
        sub.lower() in lowered
        for product_type, subs in keywords.items()
        if product_type != "digital"
        for sub in subs
    )


def _eligible(conn: sqlite3.Connection, user_id: int, cfg: OpsConfig) -> dict[str, _Target]:
    """target_id -> the eligible-to-reprice facts for that listing -- the
    ONE grounding function shared by propose() and materialize() so the two
    can never disagree, and the ONE place the digital-only/active-only
    safety line lives (both the provider-link and title guards -- see module
    docstring). Reads proj_listings (rebuilt from etsy.listing.observed, so
    `state`/`views`/`price_usd` already reflect the latest sync) rather than
    re-walking the event log itself -- same table analytics.py's own
    `_title`/`product_type_breakdown` read."""
    start = datetime.now(UTC).date() - timedelta(days=cfg.windows.revenue_window_days - 1)
    end = datetime.now(UTC).date()
    pod_linked = _pod_linked_listing_ids(conn, user_id)

    rows = conn.execute(
        "SELECT listing_id, title, views, price_usd FROM proj_listings "
        "WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall()

    out: dict[str, _Target] = {}
    for r in rows:
        if r["listing_id"] in pod_linked:
            continue  # authoritative provider signal -- never eligible, no exceptions
        if not _is_conservatively_digital(r["title"], cfg.product_type_keywords):
            # POD (canvas/acrylic/poster), unknown, or ambiguous -- draft
            # #9b: never eligible, no exceptions.
            continue
        if r["views"] < cfg.reprice.min_lifetime_views:
            continue
        sold_in_window = conn.execute(
            "SELECT 1 FROM proj_sale_items WHERE user_id=? AND listing_id=? "
            "AND sale_date BETWEEN ? AND ? LIMIT 1",
            (user_id, r["listing_id"], start.isoformat(), end.isoformat()),
        ).fetchone()
        if sold_in_window is not None:
            continue
        out[str(r["listing_id"])] = _Target(
            listing_id=r["listing_id"],
            current_price_usd=r["price_usd"],
            lifetime_views=r["views"],
        )
    return out


def _build_action(
    target: _Target,
    new_price_usd: float,
    cfg: OpsConfig,
    cfg_hash: str,
    today: str,
    expires_at: str,
) -> ProposedAction:
    raw = "|".join(
        (
            str(target.listing_id),
            str(target.current_price_usd),
            str(new_price_usd),
            str(target.lifetime_views),
        )
    )
    inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
    action_id = compute_action_id(
        "listing.reprice", str(target.listing_id), inputs_hash, cfg_hash, today
    )
    return ProposedAction(
        action_id=action_id,
        capability="listing.reprice",
        target_type="listing",
        target_id=str(target.listing_id),
        tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
        reason=(
            f"{target.lifetime_views} lifetime views, 0 sales in the last "
            f"{cfg.windows.revenue_window_days}d at ${target.current_price_usd:.2f} -- "
            f"propose ${target.current_price_usd:.2f} -> ${new_price_usd:.2f}."
        ),
        inputs_hash=inputs_hash,
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=expires_at,
        params={"price_usd": new_price_usd},
    )


def _default_proposals(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
) -> dict[str, ProposedAction]:
    targets = _eligible(conn, user_id, cfg)
    if not targets:
        return {}

    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    cfg_hash = ops_config_hash(cfg)
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()

    out: dict[str, ProposedAction] = {}
    for target_id, target in targets.items():
        new_price = max(
            cfg.reprice.min_price_usd,
            round(target.current_price_usd * (1 - cfg.reprice.default_reduction_pct), 2),
        )
        if new_price == target.current_price_usd:
            continue  # already at/below the floor -- nothing to propose
        out[target_id] = _build_action(target, new_price, cfg, cfg_hash, today, expires_at)
    return out


def _is_in_bounds_price(p: object, current_price_usd: float, cfg: OpsConfig) -> float | None:
    """Returns the validated float price, or None if `p` is anything other
    than a real, FINITE, in-bounds, changed price -- the LLM's number is
    validated against the SAME bounds a human operator would apply, never
    clamped. B1 fix: every comparison against NaN is False, so NaN must be
    rejected explicitly (`math.isfinite`) BEFORE the numeric bounds checks,
    or it would silently pass all of them and become a real price."""
    if isinstance(p, bool) or not isinstance(p, int | float):
        return None
    price = float(p)
    if not math.isfinite(price):
        return None
    lo = current_price_usd * (1 - cfg.reprice.max_pct_change)
    hi = current_price_usd * (1 + cfg.reprice.max_pct_change)
    if price < cfg.reprice.min_price_usd or price < lo or price > hi or price == current_price_usd:
        return None
    return price


class ListingReprice:
    key = "listing.reprice"
    # T2 ceiling -- NEVER promotable (Money axis = 2 by construction, draft
    # #9: a buyer who saw $12 yesterday seeing $18 today always needs an
    # operator). runner._maybe_promote enforces this Python ceiling; there
    # is no config path that can raise it.
    max_tier = Tier.PROPOSE
    policy_verified = True  # Etsy E2 permitted.

    def __init__(self, adapter: EtsyWriteAdapter) -> None:
        self._adapter = adapter

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return list(_default_proposals(conn, user_id, cfg).values())

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        target = _eligible(conn, user_id, cfg).get(intent.target_id)
        if target is None:
            return None  # ungrounded (hallucinated target, or POD/ineligible)

        price = _is_in_bounds_price(intent.params.get("price_usd"), target.current_price_usd, cfg)
        if price is None:
            return None  # out-of-bounds/non-numeric/unchanged -- dropped, never clamped

        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
        return _build_action(target, price, cfg, cfg_hash, today, expires_at)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        cfg = get_ops_config(conn, user_id)
        row = conn.execute(
            "SELECT title, state, price_usd FROM proj_listings WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"listing {listing_id}: never observed -- refusing to reprice")
        if row["state"] != "active":
            raise ValueError(f"listing {listing_id}: no longer active -- refusing to reprice")
        if listing_id in _pod_linked_listing_ids(conn, user_id):
            raise ValueError(f"listing {listing_id}: provider-linked (POD) -- refusing to reprice")
        if not _is_conservatively_digital(row["title"], cfg.product_type_keywords):
            # Guard, not a formality: a listing that went POD/unknown/
            # ambiguous between propose() and approval must never reach
            # update_listing_price -- even against a hand-forged action.
            raise ValueError(f"listing {listing_id}: not a digital listing -- refusing to reprice")

        new_price = action.params.get("price_usd")
        if new_price is None:
            raise ValueError(f"action {action.action_id}: missing params.price_usd")
        # B1 fix: re-validate against the SAME finite/bounds check
        # materialize() applies -- a hand-forged action (bypassing
        # propose()/materialize() entirely) must never reach the adapter
        # with a non-finite or out-of-bounds price either.
        validated = _is_in_bounds_price(new_price, row["price_usd"], cfg)
        if validated is None:
            raise ValueError(
                f"action {action.action_id}: params.price_usd={new_price!r} is not a finite, "
                "in-bounds price -- refusing to reprice"
            )
        new_price = validated

        before = {"price_usd": row["price_usd"]}
        self._adapter.update_listing_price(listing_id, new_price)
        after = {"price_usd": new_price}
        return ExecutionResult(before=before, after=after, cost_usd=0.0, duration_ms=0)

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        prior: dict | None = None
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                prior = e.payload["before"]
        if prior is None:
            return  # nothing was actually executed -- runner already guards this, but be safe

        listing_id = int(action.target_id)
        self._adapter.update_listing_price(listing_id, float(prior["price_usd"]))

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
