"""`listing.autorenew_off` -- the first real M8a capability (PR2, M8a spec
§4, draft §3.1 row 5). Turns OFF `should_auto_renew` on a dead, actively-
renewing listing to stop paying Etsy to renew a non-earner; `undo()` turns it
back on (this IS the "/_on" direction -- no separate proactive proposer in
this PR). Policy: Etsy E1 PERMITTED via `listings_w`, no new scope
(docs/policy/2026-08-11-autonomy-platform-policy.md). Cost: $0.00 -- turning
auto-renew OFF costs nothing (it SAVES ~$0.20/cycle), so it always passes the
default $0.00 monthly budget cap.

Objective is exactly `dead_listings() ∩ (should_auto_renew==True ∧
state=="active")` -- no expiry condition (an unverified field). A listing
that is not `state=="active"` (e.g. already expired/inactive) or already has
auto-renew off has nothing left to stop paying for and is never proposed.

Holds its own EtsyWriteAdapter, injected at construction (the chassis
contract: capabilities hold their own adapter). This module never imports or
constructs an adapter itself -- fake vs. live is entirely the caller's (CLI/
test) decision, so importing this module can never reach a live adapter."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pydantic

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListing, EtsyListingUpdate
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id


def _latest_observed(conn: sqlite3.Connection, user_id: int, listing_id: int) -> EtsyListing | None:
    """The most recent VALID etsy.listing.observed snapshot for this listing
    (last in event-id order == latest sync), or None if it was never
    observed. propose() runs unwrapped in the runner (M8a spec §3) -- one
    malformed historical row must be skipped, not crash the whole `ops run`,
    so a bad sync write can't take down an unattended autonomy pass."""
    latest: EtsyListing | None = None
    for e in read_all(conn, "etsy.listing.observed"):
        if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
            continue
        try:
            latest = EtsyListing.model_validate(e.payload)
        except pydantic.ValidationError:
            continue
    return latest


class ListingAutorenewOff:
    key = "listing.autorenew_off"
    max_tier = Tier.NOTIFY  # T1 ceiling -- ships at T2 (PROPOSE) per the chassis default.
    policy_verified = True

    def __init__(self, adapter: EtsyWriteAdapter) -> None:
        self._adapter = adapter

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        candidates = analytics.dead_listings(conn, user_id, cfg)
        if not candidates:
            return []

        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()

        actions: list[ProposedAction] = []
        for dl in candidates:
            listing = _latest_observed(conn, user_id, dl.listing_id)
            if listing is None or not listing.should_auto_renew or listing.state != "active":
                # Never observed, already auto-renew-off, or not active
                # (expired/inactive) -- nothing left to stop paying for.
                continue

            raw = "|".join(
                (
                    str(dl.listing_id),
                    str(dl.views_in_window),
                    listing.state,
                    str(listing.should_auto_renew),
                    str(dl.days_observed),
                )
            )
            inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
            action_id = compute_action_id(
                self.key, str(dl.listing_id), inputs_hash, cfg_hash, today
            )
            actions.append(
                ProposedAction(
                    action_id=action_id,
                    capability=self.key,
                    target_type="listing",
                    target_id=str(dl.listing_id),
                    tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
                    reason=(
                        f"0 sales & {dl.views_in_window} views in {dl.days_observed}d; "
                        "auto-renew on -- stop paying to renew."
                    ),
                    inputs_hash=inputs_hash,
                    estimated_cost_usd=0.0,
                    undo_available=True,
                    expires_at=expires_at,
                )
            )
        return actions

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        before = {"should_auto_renew": True}  # propose() only ever proposes should_auto_renew=True
        updated = self._adapter.update_listing(
            listing_id, EtsyListingUpdate(should_auto_renew=False)
        )
        after = {"should_auto_renew": updated.should_auto_renew}
        return ExecutionResult(before=before, after=after, cost_usd=0.0, duration_ms=0)

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        listing_id = int(action.target_id)
        self._adapter.update_listing(listing_id, EtsyListingUpdate(should_auto_renew=True))

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
