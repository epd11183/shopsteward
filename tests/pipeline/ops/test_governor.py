from datetime import date, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.governor import govern
from shopsteward.pipeline.ops.models import ProposedAction, RefusalReason, Tier
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
TODAY = date(2026, 6, 1)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _action(**overrides) -> ProposedAction:
    base = dict(
        action_id="a-1",
        capability="stub.noop",
        target_type="stub",
        target_id="t-1",
        tier=Tier.NOTIFY,
        reason="test reason.",
        inputs_hash="h",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=1)).isoformat(),
    )
    base.update(overrides)
    return ProposedAction(**base)


def _cfg():
    cfg = ops_config.load_ops_config()
    cfg.autonomy.enabled = True
    return cfg


def test_approves_when_nothing_is_wrong(conn):
    cap = StubCapability()
    decision = govern(conn, USER_ID, _action(), cap, _cfg(), TODAY)
    assert decision.approved is True
    assert decision.reason is None


def test_refuses_when_halted(conn):
    append(conn, Event(user_id=USER_ID, type="ops.halted", payload={"reason": "test"}))
    cap = StubCapability()
    decision = govern(conn, USER_ID, _action(), cap, _cfg(), TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.HALTED


def test_does_not_refuse_when_halted_then_resumed(conn):
    append(conn, Event(user_id=USER_ID, type="ops.halted", payload={"reason": "test"}))
    append(conn, Event(user_id=USER_ID, type="ops.resumed", payload={"reason": "test"}))
    cap = StubCapability()
    decision = govern(conn, USER_ID, _action(), cap, _cfg(), TODAY)
    assert decision.approved is True


def test_refuses_when_expired(conn):
    action = _action(expires_at=(TODAY - timedelta(days=1)).isoformat())
    cap = StubCapability()
    decision = govern(conn, USER_ID, action, cap, _cfg(), TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.EXPIRED


def test_refuses_when_policy_unverified(conn):
    cap = StubCapability(policy_verified=False)
    decision = govern(conn, USER_ID, _action(), cap, _cfg(), TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.POLICY_UNVERIFIED


def test_refuses_when_precondition_fails(conn):
    cap = StubCapability(precondition_ok=False)
    decision = govern(conn, USER_ID, _action(), cap, _cfg(), TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.PRECONDITION


def test_budget_refuses_any_positive_cost_at_a_zero_cap(conn):
    cfg = _cfg()
    cfg.autonomy.monthly_spend_cap_usd = 0.00
    action = _action(estimated_cost_usd=0.01)
    cap = StubCapability()
    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.BUDGET


def test_budget_passes_at_zero_cost_with_a_zero_cap(conn):
    cfg = _cfg()
    cfg.autonomy.monthly_spend_cap_usd = 0.00
    action = _action(estimated_cost_usd=0.0)
    cap = StubCapability()
    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)
    assert decision.approved is True


def test_daily_cap_refuses_once_todays_executed_count_is_met(conn):
    cfg = _cfg()
    cfg.autonomy.daily_action_cap = 1
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="action.proposed",
            payload=_action(action_id="a-0").model_dump(),
        ),
    )
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?,?,?,?)",
        (
            USER_ID,
            "action.executed",
            '{"action_id":"a-0","before":{},"after":{},"cost_usd":0.0,"duration_ms":1}',
            f"{TODAY.isoformat()}T00:00:00.000000Z",
        ),
    )
    conn.commit()
    cap = StubCapability()
    decision = govern(conn, USER_ID, _action(action_id="a-1"), cap, cfg, TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.DAILY_CAP


def test_per_capability_cap_refuses_independent_of_the_higher_daily_cap(conn):
    cfg = _cfg()
    cfg.autonomy.daily_action_cap = 10
    cfg.autonomy.per_capability_daily_cap = 1
    append(
        conn,
        Event(
            user_id=USER_ID, type="action.proposed", payload=_action(action_id="a-0").model_dump()
        ),
    )
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?,?,?,?)",
        (
            USER_ID,
            "action.executed",
            '{"action_id":"a-0","before":{},"after":{},"cost_usd":0.0,"duration_ms":1}',
            f"{TODAY.isoformat()}T00:00:00.000000Z",
        ),
    )
    conn.commit()
    cap = StubCapability()
    decision = govern(conn, USER_ID, _action(action_id="a-1"), cap, cfg, TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.PER_CAPABILITY_CAP


def test_a_refusal_always_appends_an_action_refused_event(conn):
    append(conn, Event(user_id=USER_ID, type="ops.halted", payload={"reason": "test"}))
    cap = StubCapability()
    govern(conn, USER_ID, _action(), cap, _cfg(), TODAY)

    from shopsteward.core.events import read_all

    refused = [e for e in read_all(conn, "action.refused") if e.payload["action_id"] == "a-1"]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "halted"


# --- precedence: halted beats everything else -------------------------------


def test_precedence_halted_beats_budget(conn):
    append(conn, Event(user_id=USER_ID, type="ops.halted", payload={"reason": "test"}))
    cfg = _cfg()
    action = _action(estimated_cost_usd=999.0)  # would also fail budget
    cap = StubCapability()
    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)
    assert decision.reason == RefusalReason.HALTED


def test_precedence_expired_beats_policy_unverified(conn):
    action = _action(expires_at=(TODAY - timedelta(days=1)).isoformat())
    cap = StubCapability(policy_verified=False)  # would also fail policy
    decision = govern(conn, USER_ID, action, cap, _cfg(), TODAY)
    assert decision.reason == RefusalReason.EXPIRED


def test_precedence_policy_unverified_beats_precondition(conn):
    cap = StubCapability(policy_verified=False, precondition_ok=False)
    decision = govern(conn, USER_ID, _action(), cap, _cfg(), TODAY)
    assert decision.reason == RefusalReason.POLICY_UNVERIFIED


# --- T6 (2026-08-25): listing.seo_edit's own per-listing cooldown -----------


def _seed_seo_executed(conn, action_id: str, target_id: str, day) -> None:
    """Seeds the (action.proposed, action.executed) pair `governor.py`'s
    `_capability_last_executed_at()` reads -- same synthetic shape
    test_holdout.py's own `_seed_executed()` uses, kept local here since
    that helper lives in a different test module."""
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="action.proposed",
            payload=_action(
                action_id=action_id,
                capability="listing.seo_edit",
                target_id=target_id,
            ).model_dump(),
        ),
    )
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?,?,?,?)",
        (
            USER_ID,
            "action.executed",
            f'{{"action_id":"{action_id}","before":{{}},"after":{{}},'
            f'"cost_usd":0.0,"duration_ms":1}}',
            f"{day.isoformat()}T00:00:00.000000Z",
        ),
    )
    conn.commit()


def test_seo_edit_cooldown_refuses_a_repeat_edit_inside_the_window(conn):
    cfg = _cfg()
    cfg.seo_edit.cooldown_days = 60
    _seed_seo_executed(conn, "seo-prior", "t-1", TODAY - timedelta(days=59))
    cap = StubCapability(key="listing.seo_edit")

    decision = govern(
        conn, USER_ID, _action(capability="listing.seo_edit", action_id="seo-new"), cap, cfg, TODAY
    )
    assert decision.approved is False
    assert decision.reason == RefusalReason.INELIGIBLE


def test_seo_edit_cooldown_clears_at_exactly_the_boundary(conn):
    cfg = _cfg()
    cfg.seo_edit.cooldown_days = 60
    _seed_seo_executed(conn, "seo-prior", "t-1", TODAY - timedelta(days=60))
    cap = StubCapability(key="listing.seo_edit")

    decision = govern(
        conn, USER_ID, _action(capability="listing.seo_edit", action_id="seo-new"), cap, cfg, TODAY
    )
    assert decision.approved is True


def test_seo_edit_cooldown_refusal_leaves_the_action_approvable_not_terminal(conn):
    """H1/H2a precedent: a rate/policy refusal must be `action.refused`, not
    a terminal `action.failed` -- the whole reason this lives in the
    governor rather than inside `_eligible()`/execute()."""
    cfg = _cfg()
    cfg.seo_edit.cooldown_days = 60
    _seed_seo_executed(conn, "seo-prior", "t-1", TODAY - timedelta(days=10))
    cap = StubCapability(key="listing.seo_edit")

    decision = govern(
        conn, USER_ID, _action(capability="listing.seo_edit", action_id="seo-new"), cap, cfg, TODAY
    )
    assert decision.approved is False

    refused = [e for e in read_all(conn, "action.refused") if e.payload["action_id"] == "seo-new"]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "ineligible"


def test_seo_edit_cooldown_is_per_target_not_global(conn):
    cfg = _cfg()
    cfg.seo_edit.cooldown_days = 60
    _seed_seo_executed(conn, "seo-prior", "t-1", TODAY - timedelta(days=5))
    cap = StubCapability(key="listing.seo_edit")

    decision = govern(
        conn,
        USER_ID,
        _action(capability="listing.seo_edit", action_id="seo-new", target_id="t-2"),
        cap,
        cfg,
        TODAY,
    )
    assert decision.approved is True


def test_precedence_budget_beats_daily_cap(conn):
    cfg = _cfg()
    cfg.autonomy.monthly_spend_cap_usd = 0.00
    cfg.autonomy.daily_action_cap = 1
    append(
        conn,
        Event(
            user_id=USER_ID, type="action.proposed", payload=_action(action_id="a-0").model_dump()
        ),
    )
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?,?,?,?)",
        (
            USER_ID,
            "action.executed",
            '{"action_id":"a-0","before":{},"after":{},"cost_usd":0.0,"duration_ms":1}',
            f"{TODAY.isoformat()}T00:00:00.000000Z",
        ),
    )
    conn.commit()
    action = _action(action_id="a-1", estimated_cost_usd=1.0)  # would also fail daily_cap
    cap = StubCapability()
    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)
    assert decision.reason == RefusalReason.BUDGET
