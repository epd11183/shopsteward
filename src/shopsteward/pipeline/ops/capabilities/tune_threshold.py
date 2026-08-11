"""`ops.tune_threshold` -- the feedback-loop capability (PR4, M8a spec §4,
re-aimed per the 2026-08-09 pivot: `TuningProfile`'s scoring weights are gone,
so this closes the analytics -> config loop over the surviving `ops.json`
thresholds instead. Ships T2 (PROPOSE) only -- per draft #36, silently
changing what the Brief flags as dying must always go through the operator,
so `max_tier = Tier.PROPOSE` and this capability is never promoted or
auto-executed by the runner (the ladder only ever *lowers* the ceiling below
max_tier, never raises it -- see registry.py/tiers.py).

Holds no adapter (constructed with nothing) -- it only reads `conn` and
writes `opsconfig.updated` events, so it registers in `ops run`/`approve`/
`undo` unconditionally, with no live-vs-fake adapter split at all.

The one trigger this PR ships: `dead_listing.min_observed_days` set higher
than the catalog's longest observation span makes `_dead_listing_candidates`
(analytics.py) guaranteed-empty -- no listing can ever have enough history to
be judged dead. propose()/execute() recompute the same span+new-value formula
independently (rather than carrying the new value on ProposedAction, which
has no extra-fields slot) so they can never disagree. execute() pins its
`as_of` to the action's own action.proposed event's created_at (immutable),
NOT wall-clock now() -- the observed span only grows with time, so a
same-day recompute at approval time (which may be days after propose())
would otherwise apply a larger value than the one the operator actually
saw and approved. execute() no-ops safely if the config has already moved
past the trigger since propose()."""

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.config import get_ops_config, ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id

# Only bother proposing once there is enough history to trust the span --
# a brand-new shop's catalog is naturally short-lived, not a config defect.
_MIN_SPAN_TO_TUNE = 14


def _observation_span(conn: sqlite3.Connection, user_id: int, as_of: date) -> tuple[int, int]:
    """(longest days-observed span across the user's listings, listing
    count). (0, 0) with no listings observed yet."""
    rows = conn.execute(
        "SELECT MIN(day) AS first_day FROM proj_listing_daily WHERE user_id=? GROUP BY listing_id",
        (user_id,),
    ).fetchall()
    if not rows:
        return 0, 0
    span = max((as_of - date.fromisoformat(r["first_day"])).days for r in rows)
    return span, len(rows)


def _new_min_observed(span: int) -> int:
    # ponytail: single-knob heuristic (half the observed span, floored at
    # 7d) -- the point of this PR is the governed feedback MECHANISM, not a
    # tuned formula. Add more rules/knobs later if one trigger isn't enough.
    return max(7, span // 2)


def _proposed_at(conn: sqlite3.Connection, user_id: int, action_id: str) -> date:
    """The immutable created_at date of THIS action's own action.proposed
    event -- execute() must recompute span/new_min_observed as of the day
    it was proposed, not wall-clock now(), or an operator approving days
    later gets applied a DIFFERENT (larger) value than the one named in the
    reason they approved. Falls back to wall-clock only if no proposal
    event exists (defensive -- the runner never calls execute() without one)."""
    created_at: str | None = None
    for e in read_all(conn, "action.proposed"):
        if e.user_id == user_id and e.payload.get("action_id") == action_id:
            created_at = e.created_at
    if created_at is None:
        return datetime.now(UTC).date()
    return date.fromisoformat(created_at[:10])


class OpsTuneThreshold:
    key = "ops.tune_threshold"
    max_tier = Tier.PROPOSE  # T2 ceiling -- never promoted, never auto-executed.
    policy_verified = True  # pure local config write, no external platform.

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        as_of = datetime.now(UTC).date()
        span, listing_count = _observation_span(conn, user_id, as_of)
        cur = cfg.dead_listing.min_observed_days
        if listing_count == 0 or span < _MIN_SPAN_TO_TUNE or cur <= span:
            return []

        new = _new_min_observed(span)
        if new == cur:
            return []

        today = as_of.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (as_of + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
        raw = "|".join((cfg.name, str(cur), str(span), str(new)))
        inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
        action_id = compute_action_id(self.key, cfg.name, inputs_hash, cfg_hash, today)

        return [
            ProposedAction(
                action_id=action_id,
                capability=self.key,
                target_type="ops_config",
                target_id=cfg.name,
                tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
                reason=(
                    f"Dead-listing analysis can never fire: min_observed_days={cur} exceeds "
                    f"your longest observation ({span}d); propose min_observed_days={new}."
                ),
                inputs_hash=inputs_hash,
                estimated_cost_usd=0.0,
                undo_available=True,
                expires_at=expires_at,
            )
        ]

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        as_of = _proposed_at(conn, user_id, action.action_id)
        current = get_ops_config(conn, user_id, name=action.target_id)
        span, _listing_count = _observation_span(conn, user_id, as_of)
        cur = current.dead_listing.min_observed_days
        new = _new_min_observed(span)
        before = {"min_observed_days": cur}

        if new == cur:
            # Config already moved past the trigger since propose() (edited
            # by the operator, or a prior run already applied it) -- a safe
            # no-op. Never write a stale/duplicate opsconfig.updated.
            return ExecutionResult(before=before, after=before, cost_usd=0.0, duration_ms=0)

        modified = current.model_copy(deep=True)
        modified.dead_listing.min_observed_days = new
        append(
            conn,
            Event(
                user_id=user_id,
                type="opsconfig.updated",
                payload={
                    "name": action.target_id,
                    "config": modified.model_dump(by_alias=True),
                    "source": "autonomy",
                },
            ),
        )
        after = {"min_observed_days": new}
        return ExecutionResult(before=before, after=after, cost_usd=0.0, duration_ms=0)

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        prior = None
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                prior = e.payload["before"]
        if prior is None:
            return  # nothing was actually executed -- runner already guards this, but be safe

        current = get_ops_config(conn, user_id, name=action.target_id)
        modified = current.model_copy(deep=True)
        modified.dead_listing.min_observed_days = prior["min_observed_days"]
        append(
            conn,
            Event(
                user_id=user_id,
                type="opsconfig.updated",
                payload={
                    "name": action.target_id,
                    "config": modified.model_dump(by_alias=True),
                    "source": "autonomy_undo",
                },
            ),
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
