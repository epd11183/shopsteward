"""The orchestrator: propose -> govern -> execute -> event (M8a spec §3,
draft §5/§8.1). Idempotent by action_id. This PR wires the mechanics
against zero real capabilities -- callers pass whatever `capabilities` they
want exercised (the CLI passes `registry.REGISTRY.values()`, which is empty
in PR1; tests pass a StubCapability list)."""

import logging
import sqlite3
from datetime import UTC, date, datetime

from pydantic import BaseModel

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.governor import _SEO_RENEW_CAPABILITIES, govern
from shopsteward.pipeline.ops.models import OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.projections import capability_states
from shopsteward.pipeline.ops.registry import Capability, StaleTargetError
from shopsteward.pipeline.ops.tiers import effective_tier, promote_t1_t0, promote_t2_t1

_logger = logging.getLogger(__name__)

_AUTO_EXECUTE_TIERS = (Tier.AUTO, Tier.NOTIFY)

# Finding 1: `_SEO_RENEW_CAPABILITIES` (imported from governor.py -- the one
# place that set is defined) outranks this capability for the same
# target_id within a single run() call. See run()'s own docstring.
_PIN_CAPABILITY = "social.pinterest_post"

_TERMINAL = {"executed", "refused", "rejected", "undone", "expired", "superseded"}

# Terminal for a *manual* re-approve (approve_action only): a "refused" id is
# deliberately NOT here -- an operator retrying the same action_id after a
# refusal (e.g. the halt that caused it has since been resumed) must still
# be able to re-govern it. Only a genuinely resolved id (something already
# executed/rejected/undone/failed/expired/superseded) is a no-op on a
# second approve.
_APPROVE_RESOLVED = {"executed", "rejected", "undone", "failed", "expired", "superseded"}

# E11 (root-caused further, guardrail review finding 2, 2026-08-25):
# capabilities whose `execute()`/`undo()` reaches a real external write
# surface once `--live-autonomy` is set (`cli.py`'s `_register_autorenew`
# wires each of these against `build_etsy_write_adapter(live=live_autonomy)`,
# and `run_cmd` does the same). Without the flag, that adapter is a FRESH,
# empty `FakeEtsyWriteAdapter` holding no real listing state -- running
# `execute()`/`undo()` against it doesn't harmlessly no-op, it raises
# (unknown listing_id -> EtsyWriteError 404), which `_execute_and_record`
# turns into `action.failed`, a TERMINAL state (`_APPROVE_RESOLVED` above)
# that permanently blocks any future real approval of that action_id. This
# burned the operator on 2026-08-24 via `ops approve`; E11 gated only that
# one entry point, leaving the SAME failure door open via `ops run
# --no-dry-run` (any AUTO/NOTIFY-tier action in this set) and `ops undo`
# (both reach the fake adapter identically). `_live_gate_blocks()` below is
# the one choke-point predicate `run()`, `approve_action()`, and
# `undo_action()` all check before touching an adapter for a capability in
# this set -- a refusal here leaves the action exactly as it was (still
# "proposed"/"executed", never a terminal `action.failed`/`action.undone`).
# Deliberately NOT every registered capability: `listing.gapfill_reprint`,
# `ops.tune_threshold`, `social.caption_draft`, and `social.pinterest_post`
# either hold no adapter or are ALWAYS offline by design (module
# docstrings) -- gating those too would block a legitimate, always-safe
# approval for no reason.
#
# `listing.catalog_expand` (T11, 2026-08-25 design doc §4.1) is IN this set:
# its `execute()` reaches the same real Etsy write surface (via
# `expand_one` -> `drafts.build_drafts` -> `push.push_drafts`) as the other
# members here, and an approve against a fresh `FakeEtsyWriteAdapter` would
# SUCCEED (the fake happily creates a listing) and record a bogus, terminal
# `action.executed` -- permanently blocking the real approval. Same failure
# class as the 2026-08-24 incident this set was built to prevent.
LIVE_GATED_CAPABILITIES = frozenset(
    {
        "listing.autorenew_off",
        "listing.autorenew_on",
        "listing.reprice",
        "listing.seo_edit",
        "listing.deactivate",
        "listing.renew",
        "listing.catalog_expand",
    }
)


def _live_gate_blocks(capability: str, live_autonomy: bool) -> bool:
    """The single choke-point every real execution path (`run()`,
    `approve_action()`, `undo_action()`) checks before touching an adapter
    for a `LIVE_GATED_CAPABILITIES` capability -- see that set's own
    docstring for why this must not be duplicated ad hoc per caller."""
    return not live_autonomy and capability in LIVE_GATED_CAPABILITIES


class LiveGateBlockedError(RuntimeError):
    """Raised by `undo_action()` (mirrors `RunReport.live_gate_blocked` for
    `run()`/`approve_action()`, which return rather than raise) when
    `live_autonomy=False` and the action's capability is live-gated --
    refuses before calling `cap.undo()` against a fresh, empty fake,
    leaving the executed action's state untouched (no `action.undone`, no
    demotion)."""

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        super().__init__(f"live autonomy gate not set -- refusing to undo {action_id!r}")


class RunReport(BaseModel):
    proposed: int = 0
    executed: int = 0
    refused: int = 0
    failed: int = 0
    skipped_idempotent: int = 0
    dry_run: bool = False
    # E11: set True (not counted -- this is a bool, not a tally) instead of
    # bumping `refused` when run()/approve_action() itself refuses to
    # execute against a fake adapter for a live-gated capability --
    # distinguishable from a governor refusal (no action.refused event is
    # appended; the proposal is left exactly as it was, still "proposed").
    live_gate_blocked: bool = False
    # E1: a brand-new (never-before-seen action_id) proposal was skipped
    # because a DIFFERENT, still-pending proposal already exists for the
    # same (capability, target_id) -- distinct from skipped_idempotent
    # (which is the SAME action_id resolving to itself).
    skipped_duplicate_target: int = 0
    # E1(b): pending proposals swept into a terminal `action.expired` this
    # run because `today` has passed their own `expires_at`.
    expired: int = 0


def _action_status(conn: sqlite3.Connection, user_id: int, action_id: str) -> str | None:
    """The latest action.* event type (minus the "action." prefix) recorded
    for this action_id, or None if it has never been proposed."""
    status: str | None = None
    for e in read_all(conn, "action."):
        if e.user_id != user_id or e.payload.get("action_id") != action_id:
            continue
        status = e.type.split(".", 1)[1]
    return status


# ponytail: see the matching comment on `_pending_targets` below -- this
# also calls `_action_status` (a full event-log scan) per still-pending
# proposal. Same ceiling, same upgrade path.
def _sweep_expired(conn: sqlite3.Connection, user_id: int, today: date, report: RunReport) -> None:
    """E1(b): terminalize every still-pending ("proposed", not yet approved/
    executed/etc) proposal whose own `expires_at` has passed as of `today`
    -- an `action.expired` event, folded into `_TERMINAL`/`_APPROVE_
    RESOLVED` and into `action_rows()`'s state fold. Runs at the START of
    `run()` so a stale proposal never blocks (a)'s de-dup check for the rest
    of this same call."""
    for e in read_all(conn, "action.proposed"):
        if e.user_id != user_id:
            continue
        p = e.payload
        if today <= date.fromisoformat(p["expires_at"]):
            continue
        action_id = p["action_id"]
        if _action_status(conn, user_id, action_id) != "proposed":
            continue  # already resolved some other way -- nothing to expire
        append(
            conn, Event(user_id=user_id, type="action.expired", payload={"action_id": action_id})
        )
        report.expired += 1


# ponytail: `_sweep_expired`/`_pending_targets` each call `_action_status`
# (itself a full scan of every "action." event) per still-pending proposal,
# and `_pending_targets`/`_sweep_expired` each re-scan the whole
# "action.proposed" stream independently -- O(proposals * events) per
# `run()` call. Fine at 27 listings; upgrade to a single event-ordered fold
# (projections._fold_capability_states's precedent) or a projection-backed
# lookup if the event log ever grows enough for this to show up in `ops
# run`'s wall-clock time.
def _pending_targets(
    conn: sqlite3.Connection, user_id: int, today: date
) -> dict[tuple[str, str], tuple[str, str]]:
    """E1(a): (capability, target_id) -> (action_id, inputs_hash) of the one
    still-pending ("proposed", not expired) proposal for that target, if
    any. Call AFTER `_sweep_expired()` so a just-expired id never shows up
    here. `inputs_hash` is carried alongside the action_id so a fresh
    proposal for the same target can tell an identical duplicate (finding
    3a: skip, unchanged) from a materially different one (finding 3b:
    supersede the stale pending proposal instead of silently dropping the
    more accurate new one)."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for e in read_all(conn, "action.proposed"):
        if e.user_id != user_id:
            continue
        p = e.payload
        if today > date.fromisoformat(p["expires_at"]):
            continue
        action_id = p["action_id"]
        if _action_status(conn, user_id, action_id) != "proposed":
            continue
        out[(p["capability"], str(p["target_id"]))] = (action_id, p["inputs_hash"])
    return out


def _supersede_siblings(conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
    """E1(c): once (capability, target_id) executes, terminalize any OTHER
    still-pending proposal for the SAME target as `action.superseded` --
    re-governing it later would just re-decide something already decided
    for this target this cycle. Never touches `action` itself."""
    for e in read_all(conn, "action.proposed"):
        if e.user_id != user_id:
            continue
        p = e.payload
        if p["capability"] != action.capability or str(p["target_id"]) != action.target_id:
            continue
        sibling_id = p["action_id"]
        if sibling_id == action.action_id:
            continue
        if _action_status(conn, user_id, sibling_id) != "proposed":
            continue  # already resolved some other way
        append(
            conn,
            Event(
                user_id=user_id,
                type="action.superseded",
                payload={"action_id": sibling_id, "superseded_by": action.action_id},
            ),
        )


def run(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    capabilities: list[Capability],
    *,
    dry_run: bool = False,
    today: date | None = None,
    proposals: list[ProposedAction] | None = None,
    live_autonomy: bool = False,
) -> RunReport:
    """Every capability's own `propose()` ALWAYS runs, planner or not -- a
    deterministic capability (listing.renew/autorenew_off/autorenew_on/
    deactivate/gapfill_reprint/tune_threshold) must never lose real,
    eligible candidates just because an LLM planning round didn't happen to
    name them. When `proposals` is given (the M8b planner path,
    `planner.plan_proposals()` output), it is merged in per capability, but
    ONLY for targets the capability's own `propose()` did NOT already cover:
    the planner's job (per `_build_facts_json`'s "target discovery, not
    trust" framing) is to surface targets deterministic logic MISSED, not to
    offer a competing alternative for one it already found. For a
    planner-only capability (e.g. listing.seo_edit/caption_draft, whose
    `propose()` always returns `[]`), this is simply the planner's full list.
    For `listing.reprice` -- a genuine HYBRID whose own `propose()` can
    return a real deterministic default price change for a listing the
    planner *also* independently priced -- this target_id-level filter is
    load-bearing: without it, both the deterministic and the planner's
    alternate price for the SAME listing would survive (different prices ->
    different inputs_hash -> different action_id, so a plain action_id dedup
    would not catch it), showing the operator two competing reprice
    proposals for one listing. The deterministic default always wins; the
    planner's proposal for that target_id is dropped. Any remaining
    planner-vs-deterministic exact duplicate (identical action_id) is then
    also deduped, though after the target_id filter that can only happen if
    the planner independently re-derives the identical proposal -- a subset
    case, not the primary mechanism. Everything downstream (idempotency by
    action_id, govern, T2-queue/T0-T1-execute, events) is unchanged.
    `proposals=None` (the default) is today's behavior: only `propose()`
    runs.

    Finding 1 (guardrail review, 2026-08-25): before any proposal is
    governed, a `social.pinterest_post` proposal is dropped for any
    target_id that ALSO has a `listing.seo_edit`/`listing.renew` proposal
    in this SAME `run()` call -- the documented same-run priority
    (governor.py's own docstring) that used to be approximated by a
    same-day carve-out in `govern()`'s holdout check. Resolving it here
    instead makes it deterministic regardless of capability registration
    order and independent of whichever of the two gets governed first.

    `live_autonomy` (default False, the SAFE value -- see
    `LIVE_GATED_CAPABILITIES`/`_live_gate_blocks()`) is checked for the
    AUTO/NOTIFY auto-execute path right before `action.approved` would be
    appended -- a blocked action is skipped entirely, leaving it in
    whatever state it already was ("proposed"), never `action.approved`
    with nothing to follow it."""
    report = RunReport(dry_run=dry_run)
    if not cfg.autonomy.enabled:
        return report

    today = today or datetime.now(UTC).date()
    states = capability_states(conn, user_id)

    _sweep_expired(conn, user_id, today, report)
    pending_targets = _pending_targets(conn, user_id, today)

    proposals_by_cap: dict[str, list[ProposedAction]] | None = None
    if proposals is not None:
        proposals_by_cap = {}
        for a in proposals:
            proposals_by_cap.setdefault(a.capability, []).append(a)

    # Build every capability's own+planner-merged proposal list up front
    # (one propose() call each) so the Finding 1 same-run priority filter
    # below can see the FULL picture across all capabilities before any of
    # them are governed -- deterministic regardless of `capabilities`'
    # ordering, unlike deciding it inside the per-capability loop below.
    cap_proposals_by_key: dict[str, list[ProposedAction]] = {}
    for cap in capabilities:
        own_proposals = cap.propose(conn, user_id, cfg)
        cap_proposals = own_proposals
        if proposals_by_cap is not None:
            own_action_ids = {a.action_id for a in own_proposals}
            own_target_ids = {a.target_id for a in own_proposals}
            cap_proposals = own_proposals + [
                a
                for a in proposals_by_cap.get(cap.key, [])
                if a.target_id not in own_target_ids and a.action_id not in own_action_ids
            ]
        cap_proposals_by_key[cap.key] = cap_proposals

    # Finding 1: listing.seo_edit/listing.renew outrank social.pinterest_post
    # for the SAME target_id when both are proposed in this run (governor.py
    # module docstring's documented priority) -- a pin proposal for such a
    # target is dropped here, before it (or the competing seo_edit/renew
    # proposal) ever reaches govern(), so the outcome never depends on which
    # capability happens to be governed first.
    seo_renew_targets_this_run = {
        a.target_id for key in _SEO_RENEW_CAPABILITIES for a in cap_proposals_by_key.get(key, [])
    }
    if seo_renew_targets_this_run:
        pin_proposals = cap_proposals_by_key.get(_PIN_CAPABILITY)
        if pin_proposals:
            cap_proposals_by_key[_PIN_CAPABILITY] = [
                a for a in pin_proposals if a.target_id not in seo_renew_targets_this_run
            ]

    for cap in capabilities:
        eff_tier = effective_tier(cap, states.get(cap.key))
        for action in cap_proposals_by_key.get(cap.key, []):
            action = action.model_copy(update={"tier": eff_tier})
            status = _action_status(conn, user_id, action.action_id)

            if status is None:
                # E1(a): a brand-new action_id -- but if a DIFFERENT,
                # still-pending proposal already covers this exact
                # (capability, target_id), minting a fresh one would just
                # duplicate the NEEDS-YOU queue entry for a decision that's
                # already outstanding. Skip distinctly, never propose/govern
                # -- UNLESS (finding 3b) the new proposal's inputs_hash
                # differs, meaning it's more accurate (fresh data), in which
                # case the stale pending one is superseded instead.
                existing = pending_targets.get((action.capability, action.target_id))
                if existing is not None and existing[0] != action.action_id:
                    existing_id, existing_inputs_hash = existing
                    if action.inputs_hash == existing_inputs_hash:
                        # finding 3a: an identical duplicate -- unchanged
                        # skip behavior, but now visible on replay (planner.
                        # intent_dropped / governor refusal-is-an-event
                        # precedent: "why didn't this get proposed" is
                        # answerable from the log without mutating anything).
                        append(
                            conn,
                            Event(
                                user_id=user_id,
                                type="action.proposal_deduped",
                                payload={
                                    "action_id": action.action_id,
                                    "capability": action.capability,
                                    "target_id": action.target_id,
                                    "existing_action_id": existing_id,
                                },
                            ),
                        )
                        report.skipped_duplicate_target += 1
                        continue
                    append(
                        conn,
                        Event(
                            user_id=user_id,
                            type="action.superseded",
                            payload={"action_id": existing_id, "superseded_by": action.action_id},
                        ),
                    )
                    # fall through -- (action.capability, action.target_id)
                    # now proposes/governs `action` as normal, below.

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
                pending_targets[(action.capability, action.target_id)] = (
                    action.action_id,
                    action.inputs_hash,
                )

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
            if _live_gate_blocks(action.capability, live_autonomy):
                # Finding 2: without --live-autonomy this would execute
                # against a fresh, empty fake adapter and permanently
                # terminalize the action_id on the resulting 404 -- refuse
                # BEFORE action.approved is even appended, leaving the
                # proposal exactly as it was (still "proposed").
                report.live_gate_blocked = True
                continue

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
    """H2b (guardrail review, 2026-08-25): `StaleTargetError` (registry.py's
    own docstring -- the explicit, capability-chosen opt-in) is the ONLY
    exception that terminalizes into `action.failed`. Any OTHER exception a
    capability's `execute()` raises is treated as a REFUSAL (`action.
    refused`, the same non-terminal state a `governor.govern()` refusal
    leaves an action in) -- the safe default, since an ordinary
    re-validation raise that turns out to encode a rate/policy condition
    (the failure class this guard exists for) must never permanently burn
    the action_id. A capability that genuinely needs to terminalize a stale
    target must say so explicitly by raising `StaleTargetError`."""
    try:
        result = cap.execute(conn, user_id, action)
    except StaleTargetError as exc:
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
    except Exception as exc:  # capability code is untrusted from the chassis's POV
        append(
            conn,
            Event(
                user_id=user_id,
                type="action.refused",
                payload={"action_id": action.action_id, "reason": "execute_revalidation_error"},
            ),
        )
        report.refused += 1
        _logger.warning(
            "action %s (%s): execute() raised %s during re-validation, treated as a "
            "non-terminal refusal (not action.failed) -- raise StaleTargetError instead "
            "if this really is genuine per-target staleness: %s",
            action.action_id,
            cap.key,
            type(exc).__name__,
            exc,
        )
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
    _supersede_siblings(conn, user_id, action)


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
    live_autonomy: bool = False,
) -> RunReport:
    """Operator-driven approval of a T2 proposal. Still goes through
    govern() -- draft §11's own e2e story has a manually-approved second
    action refused by the daily cap, so operator approval is a request to
    execute, not a bypass of the caps. Only on approval does
    action.approved{by:"operator"} get appended (which bumps the ladder's
    approvals counter via projections.capability_states()), then execute.
    `cfg`/`today` are optional keyword overrides for tests; callers (the
    future CLI/API) omit them and get the live config + wall-clock date.

    `live_autonomy` (E11; flipped to default False -- finding 4, the SAFE
    value -- on 2026-08-25) must be passed explicitly `True` by callers that
    want real execution against a pre-seeded `FakeEtsyWriteAdapter` (every
    capability test file's own explicit test-mode opt-in precedent).
    `cli.py`'s `approve_cmd` passes whatever `--live-autonomy` resolves to,
    which defaults to the same False -- it always wires a FRESH, empty fake
    when not set, which is never safe to execute against for
    `LIVE_GATED_CAPABILITIES` (see that set's own docstring)."""
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

    if _live_gate_blocks(action.capability, live_autonomy):
        report.live_gate_blocked = True
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
    *,
    live_autonomy: bool = False,
) -> None:
    """`live_autonomy` (finding 2; default False, the SAFE value) mirrors
    `approve_action()`'s own gate: `ops undo` reaches the SAME
    `build_etsy_write_adapter(live=live_autonomy)` construction (cli.py's
    `_register_autorenew`), so without it, `cap.undo()` against a
    `LIVE_GATED_CAPABILITIES` capability would hit a fresh, empty fake --
    raises `LiveGateBlockedError` BEFORE calling `cap.undo()`, leaving the
    executed action untouched (no `action.undone`, no demotion)."""
    action = _load_proposed_action(conn, user_id, action_id)
    cap = _find_capability(capabilities, action.capability)

    if _action_status(conn, user_id, action_id) != "executed":
        # Nothing to reverse -- never executed, or a repeat `ops undo` on an
        # id already undone. A second undo must not double cap.undo() the
        # capability or double-demote/reset its ladder counters.
        return

    if _live_gate_blocks(action.capability, live_autonomy):
        raise LiveGateBlockedError(action_id)

    if not callable(getattr(cap, "undo", None)):
        # No undo path at all (e.g. listing.gapfill_reprint -- its reversal
        # is Gate 3, declining to publish the draft). Must never crash with
        # a bare `None(...)` TypeError; the caller (ops undo CLI) surfaces
        # this as a clean, non-zero-exit message, same as an unknown
        # action_id (_find_capability's KeyError precedent) -- no partial
        # state change, no action.undone.
        raise ValueError(
            f"{cap.key} has no undo -- its reversal happens outside the autonomy "
            "chassis (e.g. decline to publish at Gate 3)."
        )

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
