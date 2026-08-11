from datetime import date

from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.models import CapabilityState, Tier
from shopsteward.pipeline.ops.tiers import (
    effective_tier,
    promote_t1_t0,
    promote_t2_t1,
    should_demote,
)
from tests.pipeline.ops.stub_capability import StubCapability

LADDER = ops_config.load_ops_config().autonomy.ladder


def test_effective_tier_of_a_fresh_capability_is_propose():
    cap = StubCapability(max_tier=Tier.AUTO)
    assert effective_tier(cap, None) == Tier.PROPOSE


def test_effective_tier_clamps_a_higher_ladder_tier_down_to_max_tier():
    cap = StubCapability(max_tier=Tier.NOTIFY)
    state = CapabilityState(capability=cap.key, tier=Tier.AUTO, tier_since="2026-01-01")
    # AUTO(0) is more autonomous than max_tier NOTIFY(1) -- must clamp UP to 1.
    assert effective_tier(cap, state) == Tier.NOTIFY


def test_effective_tier_never_exceeds_propose_even_if_max_tier_is_operator():
    cap = StubCapability(max_tier=Tier.OPERATOR)
    state = CapabilityState(capability=cap.key, tier=Tier.AUTO, tier_since="2026-01-01")
    assert effective_tier(cap, state) == Tier.PROPOSE


def test_effective_tier_passes_through_a_tier_within_range():
    cap = StubCapability(max_tier=Tier.AUTO)
    state = CapabilityState(capability=cap.key, tier=Tier.NOTIFY, tier_since="2026-01-01")
    assert effective_tier(cap, state) == Tier.NOTIFY


def test_promote_t2_t1_needs_both_the_count_and_the_clock():
    today = date(2026, 3, 1)
    enough_approvals_not_enough_days = CapabilityState(
        capability="c", approvals=LADDER.promote_approvals, tier_since="2026-02-27"
    )
    assert promote_t2_t1(enough_approvals_not_enough_days, LADDER, today) is False

    enough_days_not_enough_approvals = CapabilityState(
        capability="c", approvals=LADDER.promote_approvals - 1, tier_since="2026-01-01"
    )
    assert promote_t2_t1(enough_days_not_enough_approvals, LADDER, today) is False

    both = CapabilityState(
        capability="c", approvals=LADDER.promote_approvals, tier_since="2026-01-01"
    )
    assert promote_t2_t1(both, LADDER, today) is True


def test_promote_t2_t1_refuses_if_any_rejection_happened():
    today = date(2026, 3, 1)
    state = CapabilityState(
        capability="c", approvals=LADDER.promote_approvals, rejections=1, tier_since="2026-01-01"
    )
    assert promote_t2_t1(state, LADDER, today) is False


def test_promote_t1_t0_needs_zero_undos_count_and_clock():
    today = date(2026, 3, 1)
    state = CapabilityState(
        capability="c", executions=LADDER.t1_executions, undos=0, tier_since="2026-01-01"
    )
    assert promote_t1_t0(state, LADDER, today) is True

    with_undo = state.model_copy(update={"undos": 1})
    assert promote_t1_t0(with_undo, LADDER, today) is False


def test_should_demote_on_rejection_or_undo():
    from shopsteward.core.events import Event

    assert should_demote(Event(user_id=1, type="action.rejected", payload={})) is True
    assert should_demote(Event(user_id=1, type="action.undone", payload={})) is True
    assert should_demote(Event(user_id=1, type="action.executed", payload={})) is False
