"""`listing.deactivate` -- retire a dead listing (M8b slice 4b, design
§4/§8 slice 4, draft #7). Policy: Etsy E4 PERMITTED via `updateListing`
(state)/`listings_w`. Draft #7: R=1/A=1/M=1 -> **T1 ceiling**
(`max_tier = Tier.NOTIFY`), ships/lands at T2 per the chassis default (a
fresh capability never auto-executes on first use) -- immediately
customer-visible (the shop looks smaller) but reversible in one call, so it
is promotable to T1 on the ladder, UNLIKE `listing.reprice`/`listing.
seo_edit` (both capped at T2 forever). It HAS a working undo() (reactivate),
satisfying the registry invariant for a max_tier < PROPOSE.

**Only listing STATE (active<->inactive) is ever changed -- never title/
tags/price/SKU.** This capability calls a DEDICATED adapter method,
`update_listing_state`, not `EtsyListingUpdate` (which stays state-free) --
so SEO edit / reprice can never touch state, by construction, regardless of
what params they're handed. It is also distinct from `publish_listing`
(draft->active, PRD §13 decision 41); this is active<->inactive for an
already-live listing.

**Both digital AND POD listings are eligible** -- unlike `listing.reprice`
(digital-only, draft #9b): deactivate only flips state, never touches SKUs,
variation structure, or price, so the POD-first rule ("never modify
provider-set SKU values or variation structure") is preserved regardless of
product type (same reasoning as `listing.seo_edit`'s module docstring).

**The portfolio cap is the real control** (draft §5): the governor's
existing `weekly_catalog_pct_cap` (governor.py's PORTFOLIO_CAP refusal,
already generic across every catalog-changing capability) is what catches
"40 individually-defensible deactivations that empty the shop" -- the tier
system alone does not see that pattern. Nothing new is added here; see
tests/pipeline/ops/test_deactivate.py's portfolio-cap test for the proof.
Note: an undo does NOT free the weekly cap slot -- the append-only
action.executed event still counts toward weekly_catalog_pct_cap even after
a later action.undone. This over-counts by design (it can only tighten the
portfolio brake, never loosen it); netting reversed actions out of the cap
is deliberately not done.

`_candidates()` is the ONE grounding function shared by propose() and
materialize() (M8b slice-2 planner-safety contract, autorenew.py/reprice.py
precedent) -- eligibility is `analytics.dead_listings() ∩ (latest observed
state == "active")`; a listing already inactive/expired has nothing left to
deactivate. No params (like autorenew) -- there is nothing for an LLM to
propose beyond the target itself.

Holds its own EtsyWriteAdapter, injected at construction (the chassis
contract -- autorenew.py precedent). This module never imports or
constructs an adapter itself."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pydantic

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListing
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id


def _latest_observed(conn: sqlite3.Connection, user_id: int, listing_id: int) -> EtsyListing | None:
    """The most recent VALID etsy.listing.observed snapshot for this listing
    (autorenew.py precedent) -- one malformed historical row is skipped, not
    fatal, so a bad sync write can't take down an unattended autonomy pass."""
    latest: EtsyListing | None = None
    for e in read_all(conn, "etsy.listing.observed"):
        if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
            continue
        try:
            latest = EtsyListing.model_validate(e.payload)
        except pydantic.ValidationError:
            continue
    return latest


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
) -> dict[str, ProposedAction]:
    """target_id -> the ProposedAction propose() would build for it -- the
    ONE grounding function shared by propose() and materialize() (M8b
    slice-2 planner-safety contract) so the two can never disagree.
    Eligibility is dead ∩ observed-active, regardless of product type (both
    digital and POD are eligible -- module docstring)."""
    candidates = analytics.dead_listings(conn, user_id, cfg)
    if not candidates:
        return {}

    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    cfg_hash = ops_config_hash(cfg)
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()

    out: dict[str, ProposedAction] = {}
    for dl in candidates:
        listing = _latest_observed(conn, user_id, dl.listing_id)
        if listing is None or listing.state != "active":
            # Never observed, or already inactive/expired -- nothing left to
            # deactivate.
            continue

        raw = "|".join(
            (str(dl.listing_id), str(dl.views_in_window), str(dl.days_observed), listing.state)
        )
        inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
        action_id = compute_action_id(
            "listing.deactivate", str(dl.listing_id), inputs_hash, cfg_hash, today
        )
        out[str(dl.listing_id)] = ProposedAction(
            action_id=action_id,
            capability="listing.deactivate",
            target_type="listing",
            target_id=str(dl.listing_id),
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=f"0 views/sales in {dl.days_observed}d -- deactivate the dead listing.",
            inputs_hash=inputs_hash,
            estimated_cost_usd=0.0,
            undo_available=True,
            expires_at=expires_at,
        )
    return out


class ListingDeactivate:
    key = "listing.deactivate"
    # T1 ceiling -- ships/lands at T2 (PROPOSE) per the chassis default; a
    # fresh capability never auto-executes on first use. Promotable to T1
    # on the ladder (draft #7) -- unlike reprice/seo_edit, which cap at T2.
    max_tier = Tier.NOTIFY
    policy_verified = True  # Etsy E4 permitted.

    def __init__(self, adapter: EtsyWriteAdapter) -> None:
        self._adapter = adapter

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return list(_candidates(conn, user_id, cfg).values())

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        return _candidates(conn, user_id, cfg).get(intent.target_id)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        listing = _latest_observed(conn, user_id, listing_id)
        if listing is None or listing.state != "active":
            # Re-validate at execute time: the listing must still be
            # observed-active, or something changed it since propose() --
            # refuse rather than deactivate something already changed.
            raise ValueError(f"listing {listing_id}: no longer active -- refusing to deactivate")

        before = {"state": "active"}  # propose() only ever proposes state=="active"
        self._adapter.update_listing_state(listing_id, "inactive")
        after = {"state": "inactive"}
        return ExecutionResult(before=before, after=after, cost_usd=0.0, duration_ms=0)

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        listing_id = int(action.target_id)
        self._adapter.update_listing_state(listing_id, "active")  # exact reverse of execute()

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
