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


def test_tier_floor_starts_a_fresh_capability_at_auto_and_holds_after_demotion():
    cap = StubCapability(max_tier=Tier.AUTO)
    floor = {cap.key: int(Tier.AUTO)}
    assert effective_tier(cap, None, floor) == Tier.AUTO
    # a demotion folds a PROPOSE state (and the fold's default is PROPOSE) --
    # the floor still clamps the effective tier to AUTO.
    demoted = CapabilityState(capability=cap.key, tier=Tier.PROPOSE, tier_since="2026-01-01")
    assert effective_tier(cap, demoted, floor) == Tier.AUTO


def test_effective_tier_for_shows_the_floor_for_registered_and_ladder_otherwise(monkeypatch):
    from shopsteward.pipeline.ops import tiers

    cap = StubCapability(max_tier=Tier.AUTO)
    st = CapabilityState(capability=cap.key, tier=Tier.PROPOSE, tier_since="2026-01-01")
    floor = {cap.key: int(Tier.AUTO)}
    monkeypatch.setattr(tiers, "REGISTRY", {cap.key: cap})
    assert tiers.effective_tier_for(cap.key, st, floor) == Tier.AUTO
    monkeypatch.setattr(tiers, "REGISTRY", {})
    assert tiers.effective_tier_for(cap.key, st, floor) == Tier.PROPOSE


def test_tier_floor_can_never_raise_past_the_python_max_tier():
    # gapfill/catalog_expand-style PROPOSE ceiling: config asking for AUTO is clamped.
    cap = StubCapability(max_tier=Tier.PROPOSE)
    floor = {cap.key: int(Tier.AUTO)}
    assert effective_tier(cap, None, floor) == Tier.PROPOSE
    assert (
        effective_tier(
            cap, CapabilityState(capability=cap.key, tier=Tier.AUTO, tier_since="2026-01-01"), floor
        )
        == Tier.PROPOSE
    )


def test_shipped_tier_floor_covers_exactly_the_undoable_listing_capabilities():
    floor = ops_config.load_ops_config().autonomy.tier_floor
    assert set(floor) == {
        "listing.catalog_expand",
        "listing.seo_edit",
        "listing.size_claims_fix",
        "listing.panel_replace",
        "listing.deactivate",
        "listing.renew",
        "listing.autorenew_off",
        "social.pinterest_post",
        "social.caption_draft",
    }
    listing = {k: v for k, v in floor.items() if k.startswith("listing.")}
    assert set(listing.values()) == {int(Tier.AUTO)}
    # Buffer cutover (operator 2026-11-24): social posting starts at NOTIFY. Its
    # max_tier is NOTIFY only with a live Buffer publisher (undo = deletePost
    # while queued); without one it stays PROPOSE/draft-only (the clamp holds).
    assert floor["social.pinterest_post"] == floor["social.caption_draft"] == int(Tier.NOTIFY)
    # no-undo / non-listing capabilities are deliberately absent (stay PROPOSE);
    # autorenew_on starts recurring renewal fees the $20 cap can't see (reviewer)
    assert not {"listing.gapfill_reprint", "listing.autorenew_on"} & set(floor)
    # listing.reprice paused 2026-11-24 (it re-cut prices the operator had
    # just restored, during the sale): back to PROPOSE, operator-approved.
    assert "listing.reprice" not in floor


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
