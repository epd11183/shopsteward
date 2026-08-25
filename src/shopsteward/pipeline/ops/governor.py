"""The governor: caps, budget, portfolio %, halt check, proposal TTL, policy
and precondition checks -> approve() | refuse(reason) (M8a spec §3, draft
§5/§13.1). Pure-ish: no writes except the one it is required to make -- a
refusal is itself an event (draft §76), so "why didn't it act" is always
answerable from the log without guesswork.

Refusal precedence (first hit wins, draft §5 ordering as pinned by the PR1
contract): HALTED, EXPIRED, POLICY_UNVERIFIED, PRECONDITION, HOLDOUT,
BUDGET, DAILY_CAP, PER_CAPABILITY_CAP, PACE, INELIGIBLE, PORTFOLIO_CAP.
PACE (H1, 2026-08-25) is `listing.catalog_expand`-specific today -- see
that check's own comment below for why it lives here rather than in the
capability. INELIGIBLE (H2a, 2026-08-25, the SAME reasoning applied to
`social.caption_draft`) covers a per-target cooldown/eligibility-mode
condition -- see that check's own comment below.

E3 (holdout): `social.pinterest_post` and `listing.seo_edit`/`listing.
renew` are mutually exclusive on the SAME listing target within
`cfg.pinterest.holdout_days` -- pinning a listing that was also just
SEO-edited/renewed (or vice versa) confounds the P1 pin-experiment views
readout (2026-08-24 design doc §3), since a view-count change around
either event becomes impossible to attribute to one or the other. The two
checks read EXECUTED history (`action.executed` for seo_edit/renew,
`social.pin_drafted`/`social.pin_posted` for the pin), never proposals, so
registration order can never silently decide the outcome on its own.

Rewritten 2026-08-25 (guardrail review finding 1): the holdout window is
now FULLY SYMMETRIC at govern() time, including the SAME calendar day --
a pin executed today blocks a seo_edit/renew governed today, and a
seo_edit/renew executed today blocks a pin governed today, no exception in
either direction. There used to be a same-day carve-out here (seo_edit/
renew always won a same-day collision); it was removed because it did not
actually protect the readout in the ordering that matters in practice:
`social.pinterest_post` auto-executes in the morning `ops run`, while
`listing.seo_edit` is `Tier.PROPOSE` and gets approved by the operator
LATER THE SAME DAY, in a separate process where `run()`'s own capability
ordering is irrelevant -- the carve-out let both land on the same listing
the same day regardless. The documented priority (`listing.seo_edit`/
`listing.renew` outrank `social.pinterest_post` when BOTH would be
proposed in the same `run()` call) is instead resolved upstream, at
PROPOSE time, in `runner.run()`: a pin proposal for a target that also has
a same-run seo_edit/renew proposal is dropped before either ever reaches
`govern()`, so the priority is deterministic regardless of capability
registration order and never depends on this same-day exception."""

import sqlite3
from datetime import date

from pydantic import BaseModel

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.models import OpsConfig, ProposedAction, RefusalReason
from shopsteward.pipeline.ops.registry import Capability
from shopsteward.pipeline.ops.timeutil import parse_ts


class Decision(BaseModel):
    approved: bool
    reason: RefusalReason | None = None


def approve() -> Decision:
    return Decision(approved=True, reason=None)


def refuse(reason: RefusalReason) -> Decision:
    return Decision(approved=False, reason=reason)


def is_halted(conn: sqlite3.Connection, user_id: int) -> bool:
    """True iff the most recent ops.halted/.resumed event for this user is
    a halt with no later resume."""
    last_type: str | None = None
    for e in read_all(conn, "ops."):
        if e.user_id != user_id or e.type not in ("ops.halted", "ops.resumed"):
            continue
        last_type = e.type
    return last_type == "ops.halted"


# ponytail: every day/week/month bucketing helper below compares stored
# event `created_at` against the CALLER-SUPPLIED `today`, not against
# `created_at` itself -- correct only when `today` is the date the events
# actually append on. A backfill run or one that straddles midnight (real
# wall-clock vs. the `today` a caller pins for testing) would mis-bucket.
# Upgrade: derive every window from `created_at` directly instead of a
# passed-in `today`, if this chassis ever needs backfill/near-midnight runs.
def month_spend(conn: sqlite3.Connection, user_id: int, month_prefix: str) -> float:
    total = 0.0
    for e in read_all(conn, "action.executed"):
        if e.user_id != user_id or not (e.created_at or "").startswith(month_prefix):
            continue
        total += e.payload.get("cost_usd", 0.0)
    return total


def _capability_of_action_id(conn: sqlite3.Connection, user_id: int) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for e in read_all(conn, "action.proposed"):
        if e.user_id != user_id:
            continue
        mapping[e.payload["action_id"]] = e.payload["capability"]
    return mapping


def _proposed_target_of_action_id(conn: sqlite3.Connection, user_id: int) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for e in read_all(conn, "action.proposed"):
        if e.user_id != user_id:
            continue
        mapping[e.payload["action_id"]] = str(e.payload["target_id"])
    return mapping


_SEO_RENEW_CAPABILITIES = ("listing.seo_edit", "listing.renew")
_PIN_EVENT_TYPES = ("social.pin_drafted", "social.pin_posted")

# H1 (guardrail review, 2026-08-25, THIRD instance of this failure class):
# `listing.catalog_expand`'s own weekly pace (`cfg.catalog_expansion.
# max_new_per_week`) must be enforced HERE, not inside the capability's
# `_candidates()` -- that function is ALSO execute()'s re-validation
# predicate (registry.py's Capability-protocol/M8b slice-2 contract: one
# grounding function shared by propose()/materialize()/execute()), so a
# pace-only exclusion baked into it is indistinguishable from genuine
# staleness (file deleted, already landed, rejected) and raises a
# ValueError that runner._execute_and_record turns into a TERMINAL
# action.failed -- permanently blocking any future real approval of that
# action_id (runner.py's own LIVE_GATED_CAPABILITIES docstring names this
# exact incident class, "burned the operator on 2026-08-24"). The governor
# is the only layer that can decline without terminalizing (a refusal
# leaves the action exactly as it was, still "proposed"), so pacing lives
# here even though design §5 originally said "not a new governor concept"
# -- that call was reviewed and reversed.
_CATALOG_EXPAND_CAPABILITY = "listing.catalog_expand"

# H2a (guardrail review, 2026-08-25, the SAME failure class as H1 above,
# applied to `social.caption_draft`): cooldown and per-channel eligibility
# mode (explore/proven) are RATE/POLICY decisions, not per-target staleness
# -- caption_draft.py's own `execute()` re-validates ONLY genuine staleness
# (does the listing/channel still exist -- `caption_draft._stale_check()`),
# never cooldown/eligibility, so this is the ONE place that can decline a
# cooled-down or newly-ineligible (listing, channel) pair without
# terminalizing it. Imported locally inside `_refusal_reason()` below (not
# at module scope) to avoid a governor.py <-> capabilities/caption_draft.py
# import cycle -- capabilities modules already import governor indirectly
# via runner in some call paths.
_CAPTION_DRAFT_CAPABILITY = "social.caption_draft"

# T6 (guardrail review, 2026-08-25, the SAME failure class as H1/H2a above):
# `listing.seo_edit`'s own per-listing cooldown (`cfg.seo_edit.cooldown_days`)
# must be a governor refusal, never re-checked inside `seo_edit._eligible()`
# -- that function is ALSO execute()'s re-validation predicate, and a
# cooldown-only exclusion baked into it would be indistinguishable from
# genuine staleness there, raising a StaleTargetError/ValueError instead of
# a re-approvable refusal. Composes with, and is independent of, the E3
# pin/seo_edit HOLDOUT check above: HOLDOUT stops a pin and a seo_edit from
# BOTH landing on the SAME listing within holdout_days (confounds the pin
# readout); this cooldown instead stops `listing.seo_edit` from repeatedly
# churning the SAME listing's title/tags/description before Etsy's search
# index has had time to reflect the last edit. Different questions, same
# precedence slot (both INELIGIBLE) -- first hit still wins.
_SEO_EDIT_CAPABILITY = "listing.seo_edit"


# T11 (listing.catalog_expand, 2026-08-25 design doc §5): the portfolio cap
# below exists to stop mass CHURN of the existing catalog (repeated
# SEO-edit/reprice/deactivate cycles on listings that already exist); a
# capability that only ever ADDS a new listing is not that risk, and it is
# the only capability whose success grows the cap's own denominator
# (`_active_listing_count`). Exempting it also sidesteps a real T9-revert
# trap: at ~34 active listings, a restored `weekly_catalog_pct_cap` (e.g.
# 0.05) would silently throttle this to one execution/week with no error.
# The alternative -- tuning the cap upward for everyone -- would re-open
# churn risk on the existing catalog to solve a problem that isn't churn.
_PORTFOLIO_CAP_EXEMPT = frozenset({"listing.catalog_expand"})


# ponytail: rebuilds two full action_id -> ... maps (each a full event-log
# scan) on EVERY call, and `_refusal_reason` calls this per governed action
# -- O(events) work repeated per action per run(). Fine at 27 listings;
# upgrade to a projection-backed lookup or a single shared fold (passed in
# by the caller) if the event log ever grows enough to matter.
def _seo_renew_executed_dates(conn: sqlite3.Connection, user_id: int, target_id: str) -> list[date]:
    """E3 -- every date `listing.seo_edit`/`listing.renew` executed against
    this SAME `target_id` (a listing_id, as a string)."""
    capability_of = _capability_of_action_id(conn, user_id)
    target_of = _proposed_target_of_action_id(conn, user_id)
    out: list[date] = []
    for e in read_all(conn, "action.executed"):
        if e.user_id != user_id or not e.created_at:
            continue
        action_id = e.payload.get("action_id")
        if capability_of.get(action_id) not in _SEO_RENEW_CAPABILITIES:
            continue
        if target_of.get(action_id) != target_id:
            continue
        out.append(parse_ts(e.created_at).date())
    return out


def _capability_last_executed_at(
    conn: sqlite3.Connection, user_id: int, target_id: str, capability: str
) -> date | None:
    """T6 -- the most recent `action.executed` date for THIS `capability`
    against THIS SAME `target_id`, or None if it has never executed. Same
    two-map-then-scan shape as `_seo_renew_executed_dates()` above, narrowed
    to one capability (`_seo_renew_executed_dates()` deliberately folds
    seo_edit+renew together for the E3 holdout, a different question)."""
    capability_of = _capability_of_action_id(conn, user_id)
    target_of = _proposed_target_of_action_id(conn, user_id)
    latest: date | None = None
    for e in read_all(conn, "action.executed"):
        if e.user_id != user_id or not e.created_at:
            continue
        action_id = e.payload.get("action_id")
        if capability_of.get(action_id) != capability or target_of.get(action_id) != target_id:
            continue
        day = parse_ts(e.created_at).date()
        if latest is None or day > latest:
            latest = day
    return latest


def _pin_event_dates(conn: sqlite3.Connection, user_id: int, target_id: str) -> list[date]:
    """E3 -- every date a `social.pin_drafted`/`social.pin_posted` event
    fired for this SAME `target_id` (a listing_id, as a string)."""
    try:
        listing_id = int(target_id)
    except ValueError:
        return []
    out: list[date] = []
    for event_type in _PIN_EVENT_TYPES:
        for e in read_all(conn, event_type):
            if e.user_id != user_id or not e.created_at:
                continue
            if e.payload.get("listing_id") != listing_id:
                continue
            out.append(parse_ts(e.created_at).date())
    return out


def _holdout_blocked(dates: list[date], today: date, holdout_days: int) -> bool:
    """True iff any `dates` entry falls within `holdout_days` of `today`,
    INCLUDING today itself (fully symmetric -- see module docstring's
    2026-08-25 rewrite; never a future date, since an event can't hold out
    something that hasn't happened yet from this reader's POV)."""
    for d in dates:
        days_ago = (today - d).days
        if days_ago < 0:
            continue
        if days_ago <= holdout_days:
            return True
    return False


def _executed_since(
    conn: sqlite3.Connection, user_id: int, capability_of: dict[str, str], since_prefix: str
) -> dict[str, int]:
    """capability -> count of action.executed this user has emitted whose
    created_at starts with `since_prefix` (a day 'YYYY-MM-DD' or an ISO
    week key computed by the caller)."""
    counts: dict[str, int] = {}
    for e in read_all(conn, "action.executed"):
        if e.user_id != user_id or not (e.created_at or "").startswith(since_prefix):
            continue
        cap_key = capability_of.get(e.payload["action_id"])
        if cap_key is None:
            continue
        counts[cap_key] = counts.get(cap_key, 0) + 1
    return counts


def _executed_this_iso_week(
    conn: sqlite3.Connection, user_id: int, capability_of: dict[str, str], today: date
) -> dict[str, int]:
    year, week, _ = today.isocalendar()
    counts: dict[str, int] = {}
    for e in read_all(conn, "action.executed"):
        if e.user_id != user_id or not e.created_at:
            continue
        day = parse_ts(e.created_at).date()
        y, w, _ = day.isocalendar()
        if (y, w) != (year, week):
            continue
        cap_key = capability_of.get(e.payload["action_id"])
        if cap_key is None:
            continue
        counts[cap_key] = counts.get(cap_key, 0) + 1
    return counts


def _active_listing_count(conn: sqlite3.Connection, user_id: int) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM proj_listings WHERE user_id=? AND state='active'", (user_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        # proj_listings (core) hasn't been rebuilt yet -- treat as "no
        # catalog to protect" rather than raise; the portfolio cap simply
        # doesn't apply until there is a catalog.
        return 0
    return row[0] if row else 0


def _refusal_reason(
    conn: sqlite3.Connection,
    user_id: int,
    action: ProposedAction,
    cap: Capability,
    cfg: OpsConfig,
    today: date,
) -> RefusalReason | None:
    if is_halted(conn, user_id):
        return RefusalReason.HALTED
    if today > date.fromisoformat(action.expires_at):
        return RefusalReason.EXPIRED
    if not cap.policy_verified:
        return RefusalReason.POLICY_UNVERIFIED
    if not getattr(cap, "precondition_ok", True):
        return RefusalReason.PRECONDITION

    if action.capability == "social.pinterest_post" and _holdout_blocked(
        _seo_renew_executed_dates(conn, user_id, action.target_id),
        today,
        cfg.pinterest.holdout_days,
    ):
        return RefusalReason.HOLDOUT
    if action.capability in _SEO_RENEW_CAPABILITIES and _holdout_blocked(
        _pin_event_dates(conn, user_id, action.target_id),
        today,
        cfg.pinterest.holdout_days,
    ):
        return RefusalReason.HOLDOUT

    month_prefix = today.isoformat()[:7]
    if month_spend(conn, user_id, month_prefix) + action.estimated_cost_usd > (
        cfg.autonomy.monthly_spend_cap_usd
    ):
        return RefusalReason.BUDGET

    capability_of = _capability_of_action_id(conn, user_id)
    today_counts = _executed_since(conn, user_id, capability_of, today.isoformat())
    if sum(today_counts.values()) >= cfg.autonomy.daily_action_cap:
        return RefusalReason.DAILY_CAP
    if today_counts.get(cap.key, 0) >= cfg.autonomy.per_capability_daily_cap:
        return RefusalReason.PER_CAPABILITY_CAP

    if cap.key == _CATALOG_EXPAND_CAPABILITY:
        week_counts = _executed_this_iso_week(conn, user_id, capability_of, today)
        if week_counts.get(cap.key, 0) >= cfg.catalog_expansion.max_new_per_week:
            return RefusalReason.PACE

    if cap.key == _CAPTION_DRAFT_CAPABILITY:
        from shopsteward.pipeline.ops.capabilities.caption_draft import (
            _candidates as _caption_candidates,
        )
        from shopsteward.pipeline.ops.capabilities.caption_draft import (
            _parse_target_id as _caption_parse_target_id,
        )
        from shopsteward.pipeline.ops.capabilities.caption_draft import (
            _stale_check as _caption_stale_check,
        )

        parsed = _caption_parse_target_id(action.target_id)
        # Only refuse (never terminalize) a target that is STILL genuinely
        # grounded (`_stale_check()` -- listing exists/active, channel still
        # configured) but excluded from the full `_candidates()` set for a
        # rate/policy reason (cooldown, eligibility-mode). A malformed
        # target_id or a genuinely gone one (`_stale_check()` is None) is
        # deliberately left ALONE here -- that is `execute()`'s own job
        # (raises `StaleTargetError`, which the runner DOES terminalize;
        # H2a's whole point is separating the two, not moving every
        # rejection reason to the governor).
        if parsed is not None:
            listing_id, channel = parsed
            if _caption_stale_check(
                conn, user_id, cfg, listing_id, channel
            ) is not None and action.target_id not in _caption_candidates(conn, user_id, cfg):
                return RefusalReason.INELIGIBLE

    if cap.key == _SEO_EDIT_CAPABILITY:
        last = _capability_last_executed_at(conn, user_id, action.target_id, _SEO_EDIT_CAPABILITY)
        if last is not None and (today - last).days < cfg.seo_edit.cooldown_days:
            return RefusalReason.INELIGIBLE

    active = _active_listing_count(conn, user_id)
    if active > 0 and cap.key not in _PORTFOLIO_CAP_EXEMPT:
        week_counts = _executed_this_iso_week(conn, user_id, capability_of, today)
        projected = week_counts.get(cap.key, 0) + 1
        if projected / active > cfg.autonomy.weekly_catalog_pct_cap:
            return RefusalReason.PORTFOLIO_CAP

    return None


def govern(
    conn: sqlite3.Connection,
    user_id: int,
    action: ProposedAction,
    cap: Capability,
    cfg: OpsConfig,
    today: date,
) -> Decision:
    reason = _refusal_reason(conn, user_id, action, cap, cfg, today)
    if reason is None:
        return approve()
    append(
        conn,
        Event(
            user_id=user_id,
            type="action.refused",
            payload={"action_id": action.action_id, "reason": reason.value},
        ),
    )
    return refuse(reason)
