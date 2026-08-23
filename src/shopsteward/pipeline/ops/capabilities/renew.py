"""`listing.renew` -- bring back an expired listing that has real sales
history (M8b slice 4c). Policy: VERIFIED -- see
docs/policy/2026-08-11-autonomy-platform-policy.md entry E15 (PERMITTED,
distinguished from E8's relist-churn-for-recency prohibition on target
state, selection signal, cadence, and honesty; `listings_w`, already held,
no new scope).

There is no separate `renewListing` endpoint on Etsy's real API. Renewal IS
`PATCH .../listings/{id}` with `state=active` -- the exact same call
`update_listing_state` already makes for `listing.deactivate`'s reactivate
path -- and Etsy silently charges ~$0.20 (a real listing fee) whenever that
PATCH transitions a listing INTO `active` from `expired` (also true, less
obviously, of a fresh draft->active `publish_listing` -- not this
capability's concern). This capability therefore needs no new adapter
method: it is `update_listing_state(listing_id, "active")` called against
an `expired` listing instead of an `inactive` one.

**`max_tier = Tier.NOTIFY`** -- T1 ceiling, ships at T2 (PROPOSE) per the
chassis default (autorenew.py precedent), same as `listing.deactivate`
(T1-promotable, its own docstring): the ladder CAN eventually trust this
capability to auto-execute unattended within `_AUTO_EXECUTE_TIERS`
(runner.py), because it is a simple, mechanical state transition, unlike
`listing.seo_edit`'s permanent human-approval requirement. The real
blast-radius control once it gets there is the numeric
`autonomy.monthly_spend_cap_usd`, not the tier: every execution spends real,
non-refundable money, so it should always at minimum notify the operator
regardless of how much the ladder ever trusts it.

**Eligibility** (`_candidates()`, the ONE grounding function shared by
propose()/materialize()/execute() -- the M8b slice-2 planner-safety
contract every capability here follows): `state == "expired"` AND at least
`cfg.renew.min_lifetime_sales` real (non-fixture) sale line-items have ever
been recorded against the listing_id in `proj_sale_items` (built from
`etsy.sale.observed`, which `core/sync.py`'s `read_live_observed()` already
filters to real syncs only -- fixture-sourced events never reach this
projection) AND `quantity >= 1` (an expired listing with zero stock would
renew straight to `sold_out`, not `active`, so a zero-quantity listing is
not proposed).

**`views`/`num_favorers` are NEVER used as an eligibility signal here** --
confirmed empirically that Etsy's live API returns `views: 0` for every
expired listing regardless of real sales history, so that field carries no
information for this capability's eligibility question.

**`propose()` is deterministic and non-empty** -- unlike `listing.seo_edit`
(planner-only, `[]`), there is nothing for an LLM to generate: the only
decision this capability makes is "renew, yes or no", which `_candidates()`
already answers. One `ProposedAction` per eligible listing, built directly
from `_candidates()`.

**`materialize()` takes no params** (like `listing.deactivate`/
`listing.autorenew_off`) -- even though `propose()` already returns the
real actions, `materialize()` still exists per the Capability protocol, and
simply looks the target up in the same `_candidates()` dict.

**`undo()` is NOT a true reverse, and must never be presented as making the
operator whole.** Etsy's `state` enum for a live listing is `active`/
`inactive` only -- there is no API to restore `expired`, and the $0.20 fee
is spent and non-refundable the instant execute() runs, regardless of
whether undo() is ever called. `undo_available=True` on the `ProposedAction`
is about VISIBILITY ONLY (undo makes the listing non-public again, the same
lever `listing.deactivate` uses) -- it is not a refund and not a state
restoration, and the registry's "undo required above T2" invariant is
satisfied by that visibility lever alone, not by any claim of full reversal.

Holds its own EtsyWriteAdapter, injected at construction (the chassis
contract -- autorenew.py/deactivate.py precedent). This module never
imports or constructs an adapter itself."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pydantic

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListing
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id


def _latest_observed(conn: sqlite3.Connection, user_id: int, listing_id: int) -> EtsyListing | None:
    """The most recent VALID etsy.listing.observed snapshot for this listing
    (deactivate.py/seo_edit.py precedent) -- one malformed historical row is
    skipped, not fatal, so a bad sync write can't take down an unattended
    autonomy pass."""
    latest: EtsyListing | None = None
    for e in read_live_observed(conn, "etsy.listing.observed"):
        if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
            continue
        try:
            latest = EtsyListing.model_validate(e.payload)
        except pydantic.ValidationError:
            continue
    return latest


def _lifetime_sales_by_listing(conn: sqlite3.Connection, user_id: int) -> dict[int, int]:
    """listing_id -> count of real (non-fixture) sale line-items ever
    recorded for it, from proj_sale_items (folded from etsy.sale.observed --
    see projections.py's module docstring)."""
    rows = conn.execute(
        "SELECT listing_id, COUNT(*) AS n FROM proj_sale_items WHERE user_id=? GROUP BY listing_id",
        (user_id,),
    ).fetchall()
    return {r["listing_id"]: r["n"] for r in rows}


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
) -> dict[str, ProposedAction]:
    """target_id -> the ProposedAction propose() would build for it -- the
    ONE grounding function shared by propose()/materialize()/execute() (M8b
    slice-2 planner-safety contract) so all three can never disagree."""
    sales_by_listing = _lifetime_sales_by_listing(conn, user_id)
    if not sales_by_listing:
        return {}

    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    cfg_hash = ops_config_hash(cfg)
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
    fee = cfg.renew.listing_fee_usd

    out: dict[str, ProposedAction] = {}
    for listing_id, lifetime_sales in sales_by_listing.items():
        if lifetime_sales < cfg.renew.min_lifetime_sales:
            continue
        listing = _latest_observed(conn, user_id, listing_id)
        if listing is None or listing.state != "expired" or listing.quantity < 1:
            # Never observed, not expired (nothing to renew), or no stock
            # (would renew straight to sold_out, not active).
            continue

        raw = "|".join((str(listing_id), str(lifetime_sales), listing.state, str(listing.quantity)))
        inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
        action_id = compute_action_id(
            "listing.renew", str(listing_id), inputs_hash, cfg_hash, today
        )
        out[str(listing_id)] = ProposedAction(
            action_id=action_id,
            capability="listing.renew",
            target_type="listing",
            target_id=str(listing_id),
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=(
                f"{listing.title} -- expired, {lifetime_sales} lifetime sale(s) -- renew for "
                f"${fee:.2f} (non-refundable; undo can only mark it inactive, Etsy has no API "
                "to restore 'expired' state or refund the fee)."
            ),
            inputs_hash=inputs_hash,
            estimated_cost_usd=fee,
            undo_available=True,
            expires_at=expires_at,
        )
    return out


class ListingRenew:
    key = "listing.renew"
    # T1 ceiling -- ships at T2 (PROPOSE) per the chassis default; the real
    # blast-radius control is the numeric monthly spend cap, not the tier
    # (module docstring).
    max_tier = Tier.NOTIFY
    policy_verified = True  # docs/policy/2026-08-11-autonomy-platform-policy.md E15.

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
        from shopsteward.pipeline.ops.config import get_ops_config

        listing_id = int(action.target_id)
        cfg = get_ops_config(conn, user_id)
        if action.target_id not in _candidates(conn, user_id, cfg):
            # Re-validate at execute time: something changed since propose()
            # (already renewed by someone else, sold out in the meantime,
            # etc.) -- refuse rather than spend the fee on a stale decision.
            raise ValueError(f"listing {listing_id}: no longer eligible -- refusing to renew")

        # Honest, not a true "before" snapshot: propose() only ever proposes
        # state=="expired", but undo() can only ever set "inactive" (Etsy has
        # no API to restore "expired", fee non-refundable regardless) -- this
        # string is what cli.py's `ops undo` prints verbatim as `restored to`
        # (runner.py's action.undone `restored_to`), so it must say so rather
        # than claim "expired" was restored (F1 fix; module docstring).
        before = {
            "state": "expired (fee non-refundable; undo can only set inactive, not restore expired)"
        }
        self._adapter.update_listing_state(listing_id, "active")
        after = {"state": "active"}
        return ExecutionResult(
            before=before, after=after, cost_usd=cfg.renew.listing_fee_usd, duration_ms=0
        )

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        # NOT a true reverse -- see module docstring. Etsy has no API to
        # restore "expired"; the fee is already spent and non-refundable.
        # This only makes the listing non-public again.
        listing_id = int(action.target_id)
        self._adapter.update_listing_state(listing_id, "inactive")

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return action.estimated_cost_usd
