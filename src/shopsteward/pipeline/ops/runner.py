"""The orchestrator: propose -> govern -> execute -> event (M8a spec §3,
draft §5/§8.1). Idempotent by action_id. This PR wires the mechanics
against zero real capabilities -- callers pass whatever `capabilities` they
want exercised (the CLI passes `registry.REGISTRY.values()`, which is empty
in PR1; tests pass a StubCapability list)."""

import sqlite3
from datetime import UTC, date, datetime

from pydantic import BaseModel

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.governor import govern
from shopsteward.pipeline.ops.models import OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.projections import capability_states
from shopsteward.pipeline.ops.registry import Capability
from shopsteward.pipeline.ops.tiers import effective_tier, promote_t1_t0, promote_t2_t1

_AUTO_EXECUTE_TIERS = (Tier.AUTO, Tier.NOTIFY)

_TERMINAL = {"executed", "refused", "rejected", "undone"}

# Terminal for a *manual* re-approve (approve_action only): a "refused" id is
# deliberately NOT here -- an operator retrying the same action_id after a
# refusal (e.g. the halt that caused it has since been resumed) must still
# be able to re-govern it. Only a genuinely resolved id (something already
# executed/rejected/undone/failed) is a no-op on a second approve.
_APPROVE_RESOLVED = {"executed", "rejected", "undone", "failed"}


class RunReport(BaseModel):
    proposed: int = 0
    executed: int = 0
    refused: int = 0
    failed: int = 0
    skipped_idempotent: int = 0
    dry_run: bool = False


def _action_status(conn: sqlite3.Connection, user_id: int, action_id: str) -> str | None:
    """The latest action.* event type (minus the "action." prefix) recorded
    for this action_id, or None if it has never been proposed."""
    status: str | None = None
    for e in read_all(conn, "action."):
        if e.user_id != user_id or e.payload.get("action_id") != action_id:
            continue
        status = e.type.split(".", 1)[1]
    return status


def run(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    capabilities: list[Capability],
    *,
    dry_run: bool = False,
    today: date | None = None,
    proposals: list[ProposedAction] | None = None,
) -> RunReport:
    """When `proposals` is given (the M8b planner path, `planner.plan_proposals()`
    output), the runner uses THAT list instead of calling each capability's
    own `propose()` -- everything downstream (idempotency by action_id,
    govern, T2-queue/T0-T1-execute, events) is byte-for-byte identical to the
    deterministic path. `proposals=None` (the default) is today's unchanged
    behavior."""
    report = RunReport(dry_run=dry_run)
    if not cfg.autonomy.enabled:
        return report

    today = today or datetime.now(UTC).date()
    states = capability_states(conn, user_id)

    proposals_by_cap: dict[str, list[ProposedAction]] | None = None
    if proposals is not None:
        proposals_by_cap = {}
        for a in proposals:
            proposals_by_cap.setdefault(a.capability, []).append(a)

    for cap in capabilities:
        eff_tier = effective_tier(cap, states.get(cap.key))
        cap_proposals = (
            proposals_by_cap.get(cap.key, [])
            if proposals_by_cap is not None
            else cap.propose(conn, user_id, cfg)
        )
        for action in cap_proposals:
            action = action.model_copy(update={"tier": eff_tier})
            status = _action_status(conn, user_id, action.action_id)

            if status in _TERMINAL:
                report.skipped_idempotent += 1
                continue
            if status == "proposed" and action.tier == Tier.PROPOSE:
                # still waiting on the operator -- do not re-propose or re-govern.
                report.skipped_idempotent += 1
                continue
            if status is None:
                append(
                    conn,
                    Event(user_id=user_id, type="action.proposed", payload=action.model_dump()),
                )
                report.proposed += 1

            decision = govern(conn, user_id, action, cap, cfg, today)
            if not decision.approved:
                report.refused += 1
                continue
            if action.tier not in _AUTO_EXECUTE_TIERS:
                # PROPOSE waits for the operator (approve_action/reject_action);
                # OPERATOR/T3 is never auto-executed by the chassis, full stop --
                # explicit allow-list rather than "not PROPOSE", so a future
                # effective tier can never fall through to an auto-approval.
                continue
            if dry_run:
                continue  # would-execute; never touches the capability

            by = "tier:T0" if action.tier == Tier.AUTO else "tier:T1"
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="action.approved",
                    payload={"action_id": action.action_id, "by": by},
                ),
            )
            _execute_and_record(conn, user_id, cap, action, report)

        # Promotion depends only on already-recorded approvals/executions/
        # elapsed days (tiers.py, pure) -- orthogonal to whether THIS run
        # executes anything, so it runs even under --dry-run.
        _maybe_promote(conn, user_id, cap, cfg, today)

    return report


def _execute_and_record(
    conn: sqlite3.Connection,
    user_id: int,
    cap: Capability,
    action: ProposedAction,
    report: RunReport,
) -> None:
    try:
        result = cap.execute(conn, user_id, action)
    except Exception as exc:  # capability code is untrusted from the chassis's POV
        append(
            conn,
            Event(
                user_id=user_id,
                type="action.failed",
                payload={
                    "action_id": action.action_id,
                    "stage": "execute",
                    "error": {"code": type(exc).__name__, "message": str(exc)},
                },
            ),
        )
        report.failed += 1
        return
    append(
        conn,
        Event(
            user_id=user_id,
            type="action.executed",
            payload={
                "action_id": action.action_id,
                "before": result.before,
                "after": result.after,
                "cost_usd": result.cost_usd,
                "duration_ms": result.duration_ms,
            },
        ),
    )
    report.executed += 1


def _maybe_promote(
    conn: sqlite3.Connection, user_id: int, cap: Capability, cfg: OpsConfig, today: date
) -> None:
    """T2->T1/T1->T0 promotion (draft §2.4, tiers.py). Promotion does NOT
    reset the earning counters -- only demotion does -- but tier_since
    DOES advance to `today`, so the next promotion's day-count starts
    fresh from the moment this one landed."""
    state = capability_states(conn, user_id).get(cap.key)
    if state is None:
        return
    ladder = cfg.autonomy.ladder
    to_tier: Tier | None = None
    if state.tier == Tier.PROPOSE and promote_t2_t1(state, ladder, today):
        to_tier = Tier.NOTIFY
    elif state.tier == Tier.NOTIFY and promote_t1_t0(state, ladder, today):
        to_tier = Tier.AUTO
    if to_tier is None or int(to_tier) < int(cap.max_tier):
        return  # never promote past the Python max_tier ceiling
    append(
        conn,
        Event(
            user_id=user_id,
            type="capability.promoted",
            payload={
                "capability": cap.key,
                "from_tier": int(state.tier),
                "to_tier": int(to_tier),
                "trigger": "ladder",
            },
        ),
    )


def _load_proposed_action(conn: sqlite3.Connection, user_id: int, action_id: str) -> ProposedAction:
    for e in read_all(conn, "action.proposed"):
        if e.user_id == user_id and e.payload.get("action_id") == action_id:
            return ProposedAction.model_validate(e.payload)
    raise KeyError(f"no action.proposed found for action_id={action_id!r}, user_id={user_id}")


def _find_capability(capabilities: list[Capability], key: str) -> Capability:
    for cap in capabilities:
        if cap.key == key:
            return cap
    raise KeyError(f"no registered capability with key={key!r}")


def approve_action(
    conn: sqlite3.Connection,
    user_id: int,
    action_id: str,
    capabilities: list[Capability],
    *,
    cfg: OpsConfig | None = None,
    today: date | None = None,
) -> RunReport:
    """Operator-driven approval of a T2 proposal. Still goes through
    govern() -- draft §11's own e2e story has a manually-approved second
    action refused by the daily cap, so operator approval is a request to
    execute, not a bypass of the caps. Only on approval does
    action.approved{by:"operator"} get appended (which bumps the ladder's
    approvals counter via projections.capability_states()), then execute.
    `cfg`/`today` are optional keyword overrides for tests; callers (the
    future CLI/API) omit them and get the live config + wall-clock date."""
    from shopsteward.pipeline.ops import config as ops_config

    cfg = cfg if cfg is not None else ops_config.get_ops_config(conn, user_id)
    today = today or datetime.now(UTC).date()
    action = _load_proposed_action(conn, user_id, action_id)
    cap = _find_capability(capabilities, action.capability)

    report = RunReport()
    if _action_status(conn, user_id, action_id) in _APPROVE_RESOLVED:
        # Already resolved (a prior execute/reject/undo/failure) -- a
        # second `ops approve` on the same id must not re-approve/re-
        # execute (it would double the ladder's approvals counter and
        # could self-promote a capability on operator error/replay).
        report.skipped_idempotent += 1
        return report

    decision = govern(conn, user_id, action, cap, cfg, today)
    if not decision.approved:
        report.refused += 1
        return report

    append(
        conn,
        Event(
            user_id=user_id,
            type="action.approved",
            payload={"action_id": action_id, "by": "operator"},
        ),
    )
    _maybe_promote(conn, user_id, cap, cfg, today)
    _execute_and_record(conn, user_id, cap, action, report)
    return report


def _demote(conn: sqlite3.Connection, user_id: int, capability: str) -> None:
    """tier_since for the new tier is stamped from this event's own
    created_at when projections fold it -- no `today` parameter needed."""
    states = capability_states(conn, user_id)
    current = states.get(capability)
    from_tier = current.tier if current is not None else Tier.PROPOSE
    to_tier = Tier(min(int(from_tier) + 1, int(Tier.PROPOSE)))
    append(
        conn,
        Event(
            user_id=user_id,
            type="capability.demoted",
            payload={
                "capability": capability,
                "from_tier": int(from_tier),
                "to_tier": int(to_tier),
                "trigger": "operator",
            },
        ),
    )


def reject_action(conn: sqlite3.Connection, user_id: int, action_id: str) -> None:
    """Operator-driven rejection: append action.rejected, then demote the
    capability one tier and reset every ladder counter to zero (draft §2.4,
    asymmetric/immediate)."""
    action = _load_proposed_action(conn, user_id, action_id)
    append(
        conn,
        Event(
            user_id=user_id,
            type="action.rejected",
            payload={"action_id": action_id, "by": "operator"},
        ),
    )
    _demote(conn, user_id, action.capability)


def undo_action(
    conn: sqlite3.Connection,
    user_id: int,
    action_id: str,
    capabilities: list[Capability],
) -> None:
    action = _load_proposed_action(conn, user_id, action_id)
    cap = _find_capability(capabilities, action.capability)

    if _action_status(conn, user_id, action_id) != "executed":
        # Nothing to reverse -- never executed, or a repeat `ops undo` on an
        # id already undone. A second undo must not double cap.undo() the
        # capability or double-demote/reset its ladder counters.
        return

    executed_before: dict | None = None
    for e in read_all(conn, "action.executed"):
        if e.user_id == user_id and e.payload.get("action_id") == action_id:
            executed_before = e.payload["before"]

    cap.undo(conn, user_id, action)
    append(
        conn,
        Event(
            user_id=user_id,
            type="action.undone",
            payload={"action_id": action_id, "restored_to": executed_before},
        ),
    )
    _demote(conn, user_id, action.capability)


def halt(conn: sqlite3.Connection, user_id: int, reason: str) -> None:
    append(conn, Event(user_id=user_id, type="ops.halted", payload={"reason": reason}))


def resume(conn: sqlite3.Connection, user_id: int, reason: str) -> None:
    append(conn, Event(user_id=user_id, type="ops.resumed", payload={"reason": reason}))
