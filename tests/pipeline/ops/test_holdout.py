"""E3 -- the pin/seo_edit/renew holdout in governor.govern(): pinning a
listing that was also just SEO-edited/renewed (or vice versa) confounds the
P1 pin-experiment views readout, so the two are mutually exclusive on the
SAME listing target within `cfg.pinterest.holdout_days`.

Rewritten 2026-08-25 (guardrail review finding 1): govern()'s own same-day
window is now FULLY SYMMETRIC, including today itself -- there is no more
same-day carve-out here (see governor.py's own module docstring for why).
The documented priority (seo_edit/renew outranks the pin) is instead
resolved upstream, at PROPOSE time, in runner.run() -- see the "same-run
priority" tests at the bottom of this file."""

from datetime import date, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.governor import govern
from shopsteward.pipeline.ops.models import ProposedAction, RefusalReason, Tier
from shopsteward.pipeline.ops.runner import run
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
TARGET = "501"
TODAY = date(2026, 6, 15)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _cfg(**overrides):
    cfg = ops_config.load_ops_config()
    cfg.autonomy.enabled = True
    for k, v in overrides.items():
        setattr(cfg.pinterest, k, v)
    return cfg


def _action(capability: str, action_id: str, target_id: str = TARGET) -> ProposedAction:
    return ProposedAction(
        action_id=action_id,
        capability=capability,
        target_type="listing",
        target_id=target_id,
        tier=Tier.NOTIFY,
        reason="test reason.",
        inputs_hash="h",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=30)).isoformat(),
    )


def _seed_executed(
    conn, capability: str, action_id: str, day: date, target_id: str = TARGET
) -> None:
    """A minimal action.proposed + action.executed pair, both stamped at
    `day`, for `_seo_renew_executed_dates()`'s grounding."""
    proposed = _action(capability, action_id, target_id)
    created_at = f"{day.isoformat()}T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?,?,?,?)",
        (USER_ID, "action.proposed", proposed.model_dump_json(), created_at),
    )
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?,?,?,?)",
        (
            USER_ID,
            "action.executed",
            f'{{"action_id":"{action_id}","before":{{}},"after":{{}},'
            '"cost_usd":0.0,"duration_ms":1}',
            created_at,
        ),
    )
    conn.commit()


def _seed_pin_event(conn, event_type: str, day: date, listing_id: int = int(TARGET)) -> None:
    created_at = f"{day.isoformat()}T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?,?,?,?)",
        (
            USER_ID,
            event_type,
            (
                f'{{"listing_id": {listing_id}, "title": "x", "description": "x", '
                '"alt_text": "x", "board_key": "wall_art", "destination_url": "x", '
                '"image_url": "x"}'
            ),
            created_at,
        ),
    )
    conn.commit()


# --- pin blocked by a recent seo_edit/renew execution ------------------------


@pytest.mark.parametrize("capability", ["listing.seo_edit", "listing.renew"])
def test_pin_refused_when_seo_edit_or_renew_executed_within_the_window(conn, capability):
    _seed_executed(conn, capability, "prior-1", TODAY - timedelta(days=2))
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="social.pinterest_post")
    action = _action("social.pinterest_post", "pin-1")

    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is False
    assert decision.reason == RefusalReason.HOLDOUT


def test_pin_approved_when_seo_edit_executed_outside_the_window(conn):
    _seed_executed(conn, "listing.seo_edit", "prior-1", TODAY - timedelta(days=8))
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="social.pinterest_post")
    action = _action("social.pinterest_post", "pin-1")

    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is True


def test_pin_unaffected_by_seo_edit_on_a_different_target(conn):
    _seed_executed(conn, "listing.seo_edit", "prior-1", TODAY - timedelta(days=1), target_id="999")
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="social.pinterest_post")
    action = _action("social.pinterest_post", "pin-1", target_id=TARGET)

    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is True


# --- seo_edit/renew blocked by a recent pin -----------------------------------


@pytest.mark.parametrize("capability", ["listing.seo_edit", "listing.renew"])
@pytest.mark.parametrize("pin_event_type", ["social.pin_drafted", "social.pin_posted"])
def test_seo_edit_or_renew_refused_when_pin_within_the_window(conn, capability, pin_event_type):
    _seed_pin_event(conn, pin_event_type, TODAY - timedelta(days=2))
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key=capability)
    action = _action(capability, "seo-1")

    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is False
    assert decision.reason == RefusalReason.HOLDOUT


def test_seo_edit_approved_when_pin_outside_the_window(conn):
    _seed_pin_event(conn, "social.pin_drafted", TODAY - timedelta(days=8))
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="listing.seo_edit")
    action = _action("listing.seo_edit", "seo-1")

    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is True


# --- boundary at exactly holdout_days -----------------------------------------


def test_pin_still_blocked_at_exactly_holdout_days(conn):
    _seed_executed(conn, "listing.seo_edit", "prior-1", TODAY - timedelta(days=7))
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="social.pinterest_post")
    action = _action("social.pinterest_post", "pin-1")

    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is False
    assert decision.reason == RefusalReason.HOLDOUT


def test_pin_free_one_day_past_holdout_days(conn):
    _seed_executed(conn, "listing.seo_edit", "prior-1", TODAY - timedelta(days=8))
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="social.pinterest_post")
    action = _action("social.pinterest_post", "pin-1")

    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is True


# --- finding 1: same-day is now symmetric at govern() time -------------------


def test_same_day_pin_execution_blocks_a_same_day_seo_edit_approval(conn):
    """The real ordering finding 1 is about: `social.pinterest_post`
    auto-executes in the morning `ops run`, and the operator approves
    `listing.seo_edit` (Tier.PROPOSE) LATER THE SAME DAY, in a separate
    process where run()'s own capability ordering is irrelevant. Without a
    same-day carve-out, govern() now refuses this with HOLDOUT -- the exact
    confound the holdout exists to prevent."""
    _seed_pin_event(conn, "social.pin_drafted", TODAY)
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="listing.seo_edit")

    decision = govern(
        conn, USER_ID, _action("listing.seo_edit", "seo-approved-later-today"), cap, cfg, TODAY
    )

    assert decision.approved is False
    assert decision.reason == RefusalReason.HOLDOUT


def test_same_day_seo_edit_execution_blocks_a_same_day_pin_too(conn):
    """The reverse direction, also no longer carved out."""
    _seed_executed(conn, "listing.seo_edit", "seo-today", TODAY)
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="social.pinterest_post")

    decision = govern(
        conn, USER_ID, _action("social.pinterest_post", "pin-later-today"), cap, cfg, TODAY
    )

    assert decision.approved is False
    assert decision.reason == RefusalReason.HOLDOUT


# --- finding 1: the documented priority now lives in runner.run() -----------


def test_same_run_priority_seo_edit_wins_pin_dropped_before_either_is_governed(conn):
    """When BOTH `listing.seo_edit` and `social.pinterest_post` are proposed
    for the SAME target in a single `run()` call, the pin proposal is
    dropped before either reaches govern() -- no pin action.proposed,
    action.refused, or HOLDOUT for that target this run, regardless of
    `capabilities` ordering."""
    cfg = _cfg(holdout_days=7)
    seo_cap = StubCapability(key="listing.seo_edit", targets={TARGET: {"on": True}})
    pin_cap = StubCapability(key="social.pinterest_post", targets={TARGET: {"on": True}})

    run(conn, USER_ID, cfg, [pin_cap, seo_cap], today=TODAY)

    proposed_capabilities = {e.payload["capability"] for e in read_all(conn, "action.proposed")}
    assert proposed_capabilities == {"listing.seo_edit"}
    assert read_all(conn, "action.refused") == []


def test_same_run_priority_holds_regardless_of_capability_registration_order(conn):
    cfg = _cfg(holdout_days=7)
    seo_cap = StubCapability(key="listing.seo_edit", targets={TARGET: {"on": True}})
    pin_cap = StubCapability(key="social.pinterest_post", targets={TARGET: {"on": True}})

    run(conn, USER_ID, cfg, [seo_cap, pin_cap], today=TODAY)

    proposed_capabilities = {e.payload["capability"] for e in read_all(conn, "action.proposed")}
    assert proposed_capabilities == {"listing.seo_edit"}


# --- symmetry in both registration orders -------------------------------------


@pytest.mark.parametrize("order", ["seo_first", "pin_first"])
def test_holdout_is_symmetric_regardless_of_which_capability_is_governed_first(conn, order):
    """Registering/governing seo_edit before the pin, or the pin before
    seo_edit, in a run() capabilities list must never change which one gets
    blocked -- both directions read the SAME immutable event history."""
    day_before = TODAY - timedelta(days=1)
    if order == "seo_first":
        _seed_executed(conn, "listing.seo_edit", "seo-a", day_before)
    else:
        _seed_pin_event(conn, "social.pin_drafted", day_before)

    cfg = _cfg(holdout_days=7)
    # T6 (2026-08-25): `listing.seo_edit` now also has its OWN per-listing
    # cooldown (default 60d), a genuinely different governor check than the
    # E3 pin/seo holdout this test exercises. In the "seo_first" branch the
    # seeded prior execution is itself a `listing.seo_edit` on the SAME
    # target -- pin it well under 1 day so it can never collide with THIS
    # test's holdout-symmetry assertion (day_before is exactly 1 day ago).
    cfg.seo_edit.cooldown_days = 1
    seo_cap = StubCapability(key="listing.seo_edit")
    pin_cap = StubCapability(key="social.pinterest_post")

    seo_decision = govern(conn, USER_ID, _action("listing.seo_edit", "seo-b"), seo_cap, cfg, TODAY)
    pin_decision = govern(
        conn, USER_ID, _action("social.pinterest_post", "pin-b"), pin_cap, cfg, TODAY
    )

    if order == "seo_first":
        # a seo_edit executed yesterday blocks today's pin, never the
        # reverse (there's no prior pin in this scenario).
        assert pin_decision.approved is False
        assert seo_decision.approved is True
    else:
        assert seo_decision.approved is False
        assert pin_decision.approved is True


# --- distinct refusal reason string -------------------------------------------


def test_holdout_refusal_reason_string_is_distinct_from_cap_refusals(conn):
    assert RefusalReason.HOLDOUT.value == "holdout"
    assert RefusalReason.HOLDOUT.value not in {
        RefusalReason.DAILY_CAP.value,
        RefusalReason.PER_CAPABILITY_CAP.value,
        RefusalReason.PORTFOLIO_CAP.value,
        RefusalReason.BUDGET.value,
    }


def test_holdout_refusal_appends_a_refused_event_with_the_holdout_reason(conn):
    _seed_executed(conn, "listing.seo_edit", "prior-1", TODAY - timedelta(days=1))
    cfg = _cfg(holdout_days=7)
    cap = StubCapability(key="social.pinterest_post")
    govern(conn, USER_ID, _action("social.pinterest_post", "pin-1"), cap, cfg, TODAY)

    from shopsteward.core.events import read_all

    refused = [e for e in read_all(conn, "action.refused") if e.payload["action_id"] == "pin-1"]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "holdout"
