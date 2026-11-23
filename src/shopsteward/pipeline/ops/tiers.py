"""PURE tier arithmetic -- no I/O, no event reads (draft §2.1/§2.4, M8a
spec §3). Ladder *state* (CapabilityState) is folded from the event log
elsewhere (projections.capability_states()); this module only does the
clamp/threshold/date math over values handed to it."""

from collections.abc import Mapping
from datetime import date

from shopsteward.pipeline.ops.models import CapabilityState, Tier
from shopsteward.pipeline.ops.registry import REGISTRY, Capability


def effective_tier(
    cap: Capability,
    state: CapabilityState | None,
    tier_floor: Mapping[str, int] | None = None,
) -> Tier:
    """A capability's ladder state moves within [max_tier, floor] only
    (draft §2.4). `floor` is the least-autonomous rung the ladder may start
    or end at: PROPOSE by default (T3/OPERATOR is never part of the ladder),
    or the capability's entry in config `autonomy.tier_floor` (2026-11-22:
    the operator set every `listing.*` capability's floor to AUTO, so a fresh
    capability STARTS at AUTO and a demotion-on-undo/rejection can never drop
    it below AUTO -- the ladder events still fold, the clamp just absorbs
    them). A fresh capability (no state yet) starts at its floor. The Python
    `max_tier` is still the hard ceiling: config can lower autonomy to
    PROPOSE but can never raise it past `max_tier` (registry invariant 2),
    because the floor is itself clamped to `max_tier`."""
    floor = min(
        max((tier_floor or {}).get(cap.key, int(Tier.PROPOSE)), int(cap.max_tier)),
        int(Tier.PROPOSE),
    )
    current = state.tier if state is not None else Tier(floor)
    lo, hi = sorted((floor, int(cap.max_tier)))
    return Tier(min(max(int(current), lo), hi))


def effective_tier_for(
    cap_key: str, state: CapabilityState, tier_floor: Mapping[str, int] | None
) -> Tier:
    """effective_tier() by key, for display (`ops status`/`ops brief`): the
    ladder tier alone hides the `autonomy.tier_floor` clamp. Falls back to the
    ladder tier when the capability isn't registered (no max_tier to clamp to)."""
    cap = REGISTRY.get(cap_key)
    return state.tier if cap is None else effective_tier(cap, state, tier_floor)


def _days_since(tier_since: str, today: date) -> int:
    return (today - date.fromisoformat(tier_since)).days


def promote_t2_t1(state: CapabilityState, ladder_cfg: object, today: date) -> bool:
    """T2 -> T1: >= promote_approvals operator approvals, ZERO rejections,
    AND >= promote_min_days elapsed since tier_since. Both the count and
    the clock are required -- a batch of approvals in one sitting must not
    promote (draft §2.4)."""
    return (
        state.approvals >= ladder_cfg.promote_approvals
        and state.rejections == 0
        and _days_since(state.tier_since, today) >= ladder_cfg.promote_min_days
    )


def promote_t1_t0(state: CapabilityState, ladder_cfg: object, today: date) -> bool:
    """T1 -> T0: >= t1_executions notified executions, ZERO undos, AND
    >= t1_min_days elapsed."""
    return (
        state.executions >= ladder_cfg.t1_executions
        and state.undos == 0
        and _days_since(state.tier_since, today) >= ladder_cfg.t1_min_days
    )


def should_demote(event: object) -> bool:
    """Demotion is immediate and asymmetric (draft §2.4): any operator
    rejection OR any undo drops the capability one tier and resets every
    ladder counter to zero."""
    return event.type in ("action.rejected", "action.undone")
