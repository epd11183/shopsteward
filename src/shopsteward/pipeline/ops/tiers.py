"""PURE tier arithmetic -- no I/O, no event reads (draft §2.1/§2.4, M8a
spec §3). Ladder *state* (CapabilityState) is folded from the event log
elsewhere (projections.capability_states()); this module only does the
clamp/threshold/date math over values handed to it."""

from datetime import date

from shopsteward.pipeline.ops.models import CapabilityState, Tier
from shopsteward.pipeline.ops.registry import Capability


def effective_tier(cap: Capability, state: CapabilityState | None) -> Tier:
    """A capability's ladder state moves within [max_tier, PROPOSE] only
    (draft §2.4) -- PROPOSE is the least-autonomous rung the ladder ever
    starts or ends at (T3/OPERATOR is never part of the ladder). A fresh
    capability (no state yet) starts at PROPOSE. Clamp is unordered because
    max_tier may sit on either side of PROPOSE numerically.

    No config parameter: there is no per-capability config-lowering knob
    yet (YAGNI -- add one when the first real capability needs it); the
    only ceiling that exists today is the Python `max_tier` enforced here."""
    current = state.tier if state is not None else Tier.PROPOSE
    lo, hi = sorted((int(Tier.PROPOSE), int(cap.max_tier)))
    return Tier(min(max(int(current), lo), hi))


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
