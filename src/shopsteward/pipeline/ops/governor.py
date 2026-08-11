"""The governor: caps, budget, portfolio %, halt check, proposal TTL, policy
and precondition checks -> approve() | refuse(reason) (M8a spec §3, draft
§5/§13.1). Pure-ish: no writes except the one it is required to make -- a
refusal is itself an event (draft §76), so "why didn't it act" is always
answerable from the log without guesswork.

Refusal precedence (first hit wins, draft §5 ordering as pinned by the PR1
contract): HALTED, EXPIRED, POLICY_UNVERIFIED, PRECONDITION, BUDGET,
DAILY_CAP, PER_CAPABILITY_CAP, PORTFOLIO_CAP."""

import sqlite3
from datetime import date

from pydantic import BaseModel

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.models import OpsConfig, ProposedAction, RefusalReason
from shopsteward.pipeline.ops.registry import Capability


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
        day = date.fromisoformat(e.created_at[:10])
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

    active = _active_listing_count(conn, user_id)
    if active > 0:
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
