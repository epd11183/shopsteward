"""`listing.autorenew_on` -- turns ON `should_auto_renew` for an active
listing that currently has it off, but ONLY for listings whose MOST RECENT
renewal-posture action (`listing.renew`, `listing.autorenew_off`, or
`listing.autorenew_on` itself) was a real `listing.renew` `action.executed`
event. Needed because `listing.autorenew_off`'s own `undo()` already flips
the flag back on, but ONLY for a listing that has a prior `autorenew_off`
`action.executed` event in this system to undo -- listings whose auto-renew
was off from the start (e.g. brought back via `listing.renew`; that's why
they expired in the first place) have no such event, so `undo()` is
unreachable for them.

**Why this is NOT a pure structural inverse of `autorenew_off` (oscillation
bug, fixed here).** An earlier version of this capability used the naive
inverse eligibility -- `state == "active" AND should_auto_renew == False`,
with no further gate. That forms a stable, unattended oscillation loop with
`autorenew_off`: `autorenew_off` turns `should_auto_renew` off on a DEAD
listing (`analytics.dead_listings()`: 0 sales in its window, flat views);
the very next sync then observes exactly
`state == "active" AND should_auto_renew == False` on that same dead
listing, which was this capability's entire eligibility bar -- so it
immediately proposed turning it back on, arguing for the exact state
`autorenew_off` just prevented. A *lifetime sales-count* gate (an earlier
attempted fix) did NOT close this: `dead_listings()`'s "0 sales" test is
WINDOWED (`cfg.dead_listing.window_days`), while a lifetime sales count is
an orthogonal axis -- a listing that sold once 400+ days ago and has been
dead ever since satisfies BOTH "dead" (0 sales in-window) AND "has lifetime
sales" simultaneously, so the loop still fired for exactly the real
`LISTING_DEAD`-shaped case a shop actually has.

**A second, purely EXISTENTIAL "has it ever been renewed" gate (another
earlier attempted fix) also oscillated.** A listing renewed once (real
`listing.renew` execution), then turned back on, can independently go dead
again while still actively auto-renewing -- at which point `autorenew_off`
correctly flips it off again (it has no exclusion for previously-renewed
listings, and shouldn't need one). An existential check has no expiry: the
old renew event never stops counting, so this capability would immediately
re-propose turning it back on, right after `autorenew_off` just turned it
off. Reproduced with only legitimate history: one real `listing.renew`
execution in the past, then repeated off/on/off/on.

**The real fix: gate on RECENCY, not existence -- the ORDINAL "most recent
renewal-posture action" check.** For a given `listing_id`, find whichever of
`listing.renew`, `listing.autorenew_off`, `listing.autorenew_on`'s
`action.executed` events is the MOST RECENT one (by event id / insertion
order) for that listing_id. `autorenew_on` is only eligible for a
listing_id when that most-recent action is `listing.renew`. This is
self-disarming and provably terminating:

- After `autorenew_on` executes on a listing -> its own most-recent action
  becomes `autorenew_on` -> it immediately becomes ineligible for itself
  again (correctly refuses to re-propose the same listing pointlessly).
- After `autorenew_off` executes on a listing (including one that was
  previously renewed, then went dead again while active) -> most-recent
  action is `autorenew_off` -> `autorenew_on` correctly refuses it. The
  system's most recent explicit judgment (off) stands until something newer
  (a fresh renew) overrides it.
- Only a GENUINELY FRESH `listing.renew` execution (more recent than any
  prior on/off action for that same listing) re-arms eligibility.
- Resting state for a dead-and-off listing is: stays off. Correct, final,
  no loop.

**Why this is provably disjoint from `autorenew_off`'s eligibility at the
moment each proposes.** `autorenew_off` only ever targets a listing that is
CURRENTLY `state == "active" AND should_auto_renew == True`. The instant
`autorenew_off` executes on a listing, that listing's most-recent
renewal-posture action becomes `listing.autorenew_off`, which is never
`listing.renew` -- so this capability's ordinal gate excludes it on the very
next cycle, regardless of whether that listing was ever renewed before.
Only a fresh `listing.renew` execution, strictly after that `autorenew_off`,
can re-arm it -- and that is exactly the case this capability exists to
handle.

**Additional exclusion: currently-dead listings are never eligible, even if
freshly renewed (operator decision).** `_candidates()` also excludes any
listing_id currently in `analytics.dead_listings()`'s result, on top of the
ordinal gate above. `listing.renew`'s eligibility looks at LIFETIME sales
(no window), while `dead_listings()` looks at a WINDOWED "0 sales, flat/
declining views" test -- a listing can satisfy both at once (e.g. it sold
once 400+ days ago, was renewed on the strength of that old sale, and has
been dead ever since by the windowed test). Turning auto-renew on for such a
listing just causes a pointless on-then-off round trip -- two real Etsy
writes and two operator approvals for a net-zero outcome -- once
`autorenew_off` catches it on the very next cycle. This is layered ON TOP OF
the ordinal check, not a replacement for it: a listing can pass the ordinal
gate and still be excluded here for being currently dead.

Policy: Etsy E1 (docs/policy/2026-08-11-autonomy-platform-policy.md) covers
this. E1's analysis is about the MECHANISM -- a `should_auto_renew` flag
flip via `updateListing`, `listings_w` only, no new scope, reversible,
touches no customer -- not about which direction the flag moves; the same
PATCH call and the same scope apply whether the flag goes False->True or
True->False. `policy_verified = True`, same as `autorenew_off`.

Cost: $0.00, but NOT symmetric with `autorenew_off`'s own $0.00 reasoning.
`autorenew_off` at $0.00 defers a SAVING -- worst case, it simply spends
nothing extra later. `autorenew_on` at $0.00 instead COMMITS the listing to
recurring, ongoing future spend (~$0.20 every ~4 months, indefinitely,
unless something turns it off again) that `monthly_spend_cap_usd` is
structurally blind to: the cap only ever sees this PATCH's own $0.00, never
the downstream Etsy-initiated renewal charges it sets in motion. The $0.00
value itself is still correct -- this PATCH doesn't itself charge anything,
the future renewal is a separate event outside this system's control -- but
the two capabilities' $0.00s do not mean the same thing, and this docstring
previously implied a false symmetry.

`max_tier = Tier.NOTIFY` -- same T1 ceiling as `autorenew_off`: this is the
same class of action (a free, reversible, single-field state flip), just
the opposite direction.

Holds its own EtsyWriteAdapter, injected at construction (the chassis
contract: capabilities hold their own adapter). This module never imports
or constructs an adapter itself -- fake vs. live is entirely the caller's
(CLI/test) decision, so importing this module can never reach a live
adapter."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pydantic

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListing, EtsyListingUpdate
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import read_all
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id


def _latest_observed(conn: sqlite3.Connection, user_id: int, listing_id: int) -> EtsyListing | None:
    """The most recent VALID etsy.listing.observed snapshot for this listing
    (autorenew.py precedent) -- one malformed historical row is skipped, not
    fatal, so a bad sync write can't take down an unattended autonomy pass."""
    latest: EtsyListing | None = None
    for e in read_live_observed(conn, "etsy.listing.observed"):
        if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
            continue
        try:
            latest = EtsyListing.model_validate(e.payload)
        except pydantic.ValidationError:
            continue
    return latest


_RENEWAL_POSTURE_CAPABILITIES = {"listing.renew", "listing.autorenew_off", "listing.autorenew_on"}


def _listings_most_recently_renewed(conn: sqlite3.Connection, user_id: int) -> set[int]:
    """listing_ids whose MOST RECENT renewal-posture `action.executed` event
    (among `listing.renew`, `listing.autorenew_off`, `listing.autorenew_on`,
    ordered by event id -- `read_all()` returns events in `id` order) was
    specifically `listing.renew`. Ordinal, not existential (module
    docstring): a listing keeps only its LATEST verdict, so `autorenew_off`
    or `autorenew_on` executing on a listing always overrides -- and
    disarms -- any earlier `listing.renew` for that same listing_id.

    `action.executed` payloads only carry `action_id` (runner.py's
    `_execute_and_record`) -- capability/target_id live on the matching
    `action.proposed` event (payload = the full `ProposedAction.model_dump()`,
    runner.py's `run()`), so this joins the two by `action_id`. Uses `.get()`
    defensively throughout (like `_latest_observed()` above): `propose()`
    runs unwrapped in the runner -- one malformed historical event row must
    be skipped, not crash the whole ops run."""
    proposed_by_id: dict[str, tuple[str, str]] = {}
    for e in read_all(conn, "action.proposed"):
        if e.user_id != user_id:
            continue
        action_id = e.payload.get("action_id")
        capability = e.payload.get("capability")
        target_id = e.payload.get("target_id")
        if action_id is None or capability is None or target_id is None:
            continue
        proposed_by_id[action_id] = (capability, target_id)

    latest_capability_by_listing: dict[int, str] = {}
    for e in read_all(conn, "action.executed"):
        if e.user_id != user_id:
            continue
        action_id = e.payload.get("action_id")
        if action_id is None:
            continue
        matched = proposed_by_id.get(action_id)
        if matched is None:
            continue
        capability, target_id = matched
        if capability not in _RENEWAL_POSTURE_CAPABILITIES:
            continue
        try:
            listing_id = int(target_id)
        except (TypeError, ValueError):
            continue
        # read_all() is id-ordered, so the last write for a listing_id wins.
        latest_capability_by_listing[listing_id] = capability

    return {
        listing_id
        for listing_id, capability in latest_capability_by_listing.items()
        if capability == "listing.renew"
    }


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
) -> dict[str, ProposedAction]:
    """target_id -> the ProposedAction propose() would build for it -- the
    ONE grounding function shared by propose()/materialize()/execute() (M8b
    slice-2 planner-safety contract) so all three can never disagree.

    Eligibility: `state == "active"` AND `should_auto_renew == False` AND
    this listing_id's MOST RECENT renewal-posture `action.executed` event
    was `listing.renew` (module docstring: the ordinal check that closes
    the oscillation loop with `ListingAutorenewOff`, structurally) AND the
    listing_id is NOT currently in `analytics.dead_listings()` (module
    docstring: an additional exclusion, layered on top of the ordinal check,
    that closes the on-then-off round trip on a listing that's still dead
    by the separate windowed check even though it was renewed on the
    strength of older lifetime sales history). Starts from `proj_listings`'
    `state='active'` rows (seo_edit.py precedent) -- `should_auto_renew`
    isn't carried by that projection, so each candidate still needs
    `_latest_observed()` to read the flag."""
    most_recently_renewed_ids = _listings_most_recently_renewed(conn, user_id)
    if not most_recently_renewed_ids:
        return {}

    dead_ids = {d.listing_id for d in analytics.dead_listings(conn, user_id, cfg)}

    active_rows = conn.execute(
        "SELECT listing_id FROM proj_listings WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall()
    if not active_rows:
        return {}

    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    cfg_hash = ops_config_hash(cfg)
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()

    out: dict[str, ProposedAction] = {}
    for row in active_rows:
        listing_id = row["listing_id"]
        if listing_id not in most_recently_renewed_ids:
            # Most recent renewal-posture action for this listing_id was NOT
            # listing.renew (never renewed, or a later autorenew_off/_on
            # action superseded an old renew) -- the ordinal oscillation
            # guard (module docstring).
            continue
        if listing_id in dead_ids:
            # Currently dead by the separate windowed check -- turning
            # auto-renew on here just sets up a pointless on-then-off round
            # trip once autorenew_off catches it next cycle (module
            # docstring, operator decision).
            continue
        listing = _latest_observed(conn, user_id, listing_id)
        if listing is None or listing.should_auto_renew or listing.state != "active":
            # Never observed, already auto-renew-on, or not active
            # (expired/inactive) -- nothing to turn on here.
            continue

        raw = "|".join((str(listing_id), listing.state, str(listing.should_auto_renew)))
        inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
        action_id = compute_action_id(
            "listing.autorenew_on", str(listing_id), inputs_hash, cfg_hash, today
        )
        out[str(listing_id)] = ProposedAction(
            action_id=action_id,
            capability="listing.autorenew_on",
            target_type="listing",
            target_id=str(listing_id),
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=(
                f"{listing.title} -- active, auto-renew off, previously brought back via "
                "listing.renew -- turn it back on so it doesn't quietly expire again."
            ),
            inputs_hash=inputs_hash,
            estimated_cost_usd=0.0,
            undo_available=True,
            expires_at=expires_at,
        )
    return out


class ListingAutorenewOn:
    key = "listing.autorenew_on"
    max_tier = Tier.NOTIFY  # T1 ceiling -- ships at T2 (PROPOSE) per the chassis default.
    policy_verified = True  # E1 -- same mechanism/scope as autorenew_off, direction-agnostic.

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
            # Re-validate at execute time (renew.py precedent): a stored T2
            # proposal can sit open past a NEWER, contradicting
            # renewal-posture verdict (e.g. autorenew_off executing on the
            # same listing after this proposal was made, which flips the
            # ordinal gate against it) -- refuse rather than write a real
            # Etsy update that contradicts the system's current judgment.
            raise ValueError(
                f"listing {listing_id}: no longer eligible -- refusing to turn auto-renew back on"
            )

        before = {"should_auto_renew": False}  # propose() only proposes should_auto_renew=False
        updated = self._adapter.update_listing(
            listing_id, EtsyListingUpdate(should_auto_renew=True)
        )
        after = {"should_auto_renew": updated.should_auto_renew}
        return ExecutionResult(before=before, after=after, cost_usd=0.0, duration_ms=0)

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        listing_id = int(action.target_id)
        self._adapter.update_listing(listing_id, EtsyListingUpdate(should_auto_renew=False))

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
