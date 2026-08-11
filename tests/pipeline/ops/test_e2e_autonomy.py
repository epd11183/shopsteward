"""The smallest test that proves the chassis works (draft §11, adapted to
the PR1 contract §11): one StubCapability, entirely on fakes, zero network.
Mirrors the 8-step story: enabled-gate, propose, approve+execute, daily
cap refusal, undo, demotion, idempotent re-run, and the audit invariant
(every executed action has a preceding approved; no token/key anywhere)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.runner import approve_action, reject_action, run, undo_action
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
# Real wall-clock "today" -- events append with the DB's own now() default
# (no created_at override plumbing exists through the chassis functions),
# so every day-bucketed governor check (daily cap, budget month, portfolio
# week) must be compared against the SAME today the events actually landed
# on, not a fixed historical date.
TODAY = datetime.now(UTC).date()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    cfg.autonomy.enabled = True
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def test_1_disabled_autonomy_run_is_a_total_noop(conn):
    cfg = ops_config.load_ops_config()
    assert cfg.autonomy.enabled is False
    cap = StubCapability(targets={"111": {"on": True}, "222": {"on": True}})

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)

    assert report.proposed == 0
    assert report.executed == 0
    assert len(read_all(conn, "action.")) == 0


def test_2_enabled_capability_at_t2_proposes_but_never_executes(conn):
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing",
        max_tier=Tier.NOTIFY,
        targets={"111": {"on": True}, "222": {"on": True}},
    )

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)

    assert report.proposed == 2
    assert report.executed == 0
    proposed = [e for e in read_all(conn, "action.proposed")]
    assert len(proposed) == 2
    assert all(p.payload["tier"] == int(Tier.PROPOSE) for p in proposed)  # fresh cap starts at T2
    assert cap.execute_calls == []


def test_3_operator_approval_executes_and_mutates_the_stub(conn):
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    report = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    assert report.executed == 1
    assert cap.store["111"]["on"] is False
    assert cap.execute_calls == ["111"]
    executed = [e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id]
    assert len(executed) == 1
    assert executed[0].payload["before"] == {"on": True}
    assert executed[0].payload["after"] == {"on": False}


def test_4_second_same_day_approval_is_refused_by_the_daily_cap(conn):
    cfg = _cfg(daily_action_cap=1)
    cap = StubCapability(
        key="stub.dead_listing",
        max_tier=Tier.NOTIFY,
        targets={"111": {"on": True}, "222": {"on": True}},
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_ids = [e.payload["action_id"] for e in read_all(conn, "action.proposed")]

    # Both proposals are at T2 -- operator approves both. The first
    # executes and consumes the daily cap; the second is refused by it.
    first = approve_action(conn, USER_ID, action_ids[0], [cap], cfg=cfg, today=TODAY)
    second = approve_action(conn, USER_ID, action_ids[1], [cap], cfg=cfg, today=TODAY)

    assert first.executed == 1
    assert second.executed == 0
    refused = [
        e for e in read_all(conn, "action.refused") if e.payload["action_id"] == action_ids[1]
    ]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "daily_cap"


def test_5_undo_restores_the_stub_byte_identical(conn):
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    before_snapshot = {"on": True}
    assert cap.store["111"] != before_snapshot  # sanity: execution did mutate it

    undo_action(conn, USER_ID, action_id, [cap])

    assert cap.store["111"] == before_snapshot
    assert cap.undo_calls == ["111"]
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id]
    assert len(undone) == 1
    assert undone[0].payload["restored_to"] == before_snapshot


def test_6_undo_demotes_the_capability_and_resets_counters(conn):
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)  # bumps approvals to 1

    undo_action(conn, USER_ID, action_id, [cap])

    demoted = [e for e in read_all(conn, "capability.") if e.type == "capability.demoted"]
    assert len(demoted) == 1
    assert demoted[0].payload["capability"] == "stub.dead_listing"

    from shopsteward.pipeline.ops.projections import capability_states

    state = capability_states(conn, USER_ID)["stub.dead_listing"]
    assert state.approvals == 0
    assert state.rejections == 0
    assert state.undos == 0
    assert state.executions == 0


def test_reject_also_demotes_and_resets_counters(conn):
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    reject_action(conn, USER_ID, action_id)

    rejected = [e for e in read_all(conn, "action.rejected") if e.payload["action_id"] == action_id]
    assert len(rejected) == 1
    demoted = [e for e in read_all(conn, "capability.") if e.type == "capability.demoted"]
    assert len(demoted) == 1
    assert cap.execute_calls == []  # a rejected T2 proposal never executes


def test_7_rerunning_the_same_day_is_idempotent(conn):
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing",
        max_tier=Tier.NOTIFY,
        targets={"111": {"on": True}, "222": {"on": True}},
    )
    first = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert first.proposed == 2

    second = run(conn, USER_ID, cfg, [cap], today=TODAY)

    assert second.proposed == 0
    assert second.skipped_idempotent == 2
    assert len(read_all(conn, "action.proposed")) == 2  # no duplicates
    assert cap.execute_calls == []


def test_8_every_executed_action_has_a_preceding_approved_and_no_secret_in_any_payload(conn):
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    events = read_all(conn)
    # First (lowest id) action.approved event per action_id -- the actual
    # invariant is ORDER, not just membership: approved must precede
    # executed in the event log, not merely exist somewhere in it.
    first_approved_id: dict[str, int] = {}
    for e in events:
        if e.type == "action.approved":
            first_approved_id.setdefault(e.payload["action_id"], e.id)
    executed = [e for e in events if e.type == "action.executed"]
    assert len(executed) >= 1
    for e in executed:
        approved_id = first_approved_id.get(e.payload["action_id"])
        assert approved_id is not None, f"executed {e.payload['action_id']} has no approved event"
        assert approved_id < e.id, "action.approved must precede action.executed in event order"

    banned = ("token", "api_key", "apikey", "secret", "signed_url", "access_token", "refresh_token")
    for e in events:
        blob = json.dumps(e.payload).lower()
        for word in banned:
            assert word not in blob, f"event {e.type} payload leaked {word!r}: {blob}"


def test_fold_resets_counters_only_at_the_demotion_point_not_across_all_history(conn):
    """Regression for the two-pass fold bug: approve A (approvals -> 1),
    reject B (demote + reset -> approvals 0), approve C (approvals -> 1
    again). A correct single-pass fold ends at approvals=1, not 2 (the old
    two-pass fold summed ALL approvals across history, then zeroed
    whatever the running total was -- coincidentally 0 when nothing
    followed the one demotion in the earlier tests)."""
    cfg = _cfg()
    cap = StubCapability(
        key="stub.fold_regression",
        max_tier=Tier.NOTIFY,
        targets={"A": {"on": True}, "B": {"on": True}, "C": {"on": True}},
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    ids = {
        e.payload["target_id"]: e.payload["action_id"] for e in read_all(conn, "action.proposed")
    }

    approve_action(conn, USER_ID, ids["A"], [cap], cfg=cfg, today=TODAY)  # approvals -> 1
    reject_action(conn, USER_ID, ids["B"])  # demote + reset -> approvals 0
    approve_action(conn, USER_ID, ids["C"], [cap], cfg=cfg, today=TODAY)  # approvals -> 1 again

    from shopsteward.pipeline.ops.projections import capability_states

    state = capability_states(conn, USER_ID)["stub.fold_regression"]
    assert state.approvals == 1  # not 2 -- must not accumulate across the demotion
    assert state.tier == Tier.PROPOSE  # already at the ladder floor; demotion still recorded


def test_promotion_t2_to_t1_fires_after_enough_approvals_and_elapsed_days(conn):
    cfg = _cfg()
    cfg.autonomy.ladder.promote_approvals = 2
    cfg.autonomy.ladder.promote_min_days = 1
    cap = StubCapability(
        key="stub.promotable",
        max_tier=Tier.NOTIFY,
        targets={"111": {"on": True}, "222": {"on": True}},
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_ids = [e.payload["action_id"] for e in read_all(conn, "action.proposed")]

    approve_action(conn, USER_ID, action_ids[0], [cap], cfg=cfg, today=TODAY)
    later = TODAY + timedelta(days=1)
    approve_action(conn, USER_ID, action_ids[1], [cap], cfg=cfg, today=later)

    promoted = [e for e in read_all(conn, "capability.") if e.type == "capability.promoted"]
    assert len(promoted) == 1
    assert promoted[0].payload["capability"] == "stub.promotable"
    assert promoted[0].payload["from_tier"] == int(Tier.PROPOSE)
    assert promoted[0].payload["to_tier"] == int(Tier.NOTIFY)

    from shopsteward.pipeline.ops.projections import capability_states

    state = capability_states(conn, USER_ID)["stub.promotable"]
    assert state.tier == Tier.NOTIFY


def test_re_approving_an_already_executed_action_is_a_noop(conn):
    """Regression: approve_action had no terminal-state guard, so a second
    `ops approve <id>` on an already-executed action appended a SECOND
    action.approved{operator}/action.executed, double-counting the ladder's
    approvals counter (and could self-promote the capability on operator
    replay)."""
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    first = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    second = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    assert first.executed == 1
    assert second.executed == 0
    assert second.skipped_idempotent == 1
    assert cap.execute_calls == ["111"]  # not called twice

    executed = [e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id]
    approved = [
        e
        for e in read_all(conn, "action.approved")
        if e.payload["action_id"] == action_id and e.payload["by"] == "operator"
    ]
    assert len(executed) == 1
    assert len(approved) == 1

    from shopsteward.pipeline.ops.projections import capability_states

    assert capability_states(conn, USER_ID)["stub.dead_listing"].approvals == 1


def test_re_undoing_an_already_undone_action_is_a_noop(conn):
    """Regression: undo_action had no terminal-state guard, so a second
    `ops undo <id>` re-ran cap.undo() and appended a SECOND action.undone +
    a SECOND capability.demoted (double-demotion)."""
    cfg = _cfg()
    cap = StubCapability(
        key="stub.dead_listing", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    undo_action(conn, USER_ID, action_id, [cap])
    undo_action(conn, USER_ID, action_id, [cap])  # second call: no-op

    assert cap.undo_calls == ["111"]  # not called twice
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id]
    assert len(undone) == 1
    demoted = [e for e in read_all(conn, "capability.") if e.type == "capability.demoted"]
    assert len(demoted) == 1  # not double-demoted
