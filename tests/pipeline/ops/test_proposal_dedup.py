"""E1 -- proposal lifecycle dedup. `compute_action_id` folds `today` into
the action_id, so the same (capability, target_id) mints a FRESH pending
proposal every calendar day it's still eligible -- this file checks the
three-part fix: (a) never mint a second pending proposal for a target that
already has one outstanding, (b) sweep proposals whose expires_at has
passed into a terminal `action.expired`, (c) supersede any still-pending
sibling once one of them executes."""

from datetime import date, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.projections import action_rows
from shopsteward.pipeline.ops.runner import approve_action, run
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
DAY1 = date(2026, 1, 1)
DAY2 = date(2026, 1, 2)


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


def _state_of(conn, action_id: str) -> str:
    (row,) = [r for r in action_rows(conn) if r["action_id"] == action_id]
    return row["state"]


# --- (a) no duplicate pending proposal across days --------------------------


def test_two_runs_on_consecutive_days_yield_exactly_one_pending_proposal(conn):
    cfg = _cfg()
    cap = StubCapability(key="stub.dedup", day=DAY1, targets={"111": {"on": True}})

    first = run(conn, USER_ID, cfg, [cap], today=DAY1)
    assert first.proposed == 1
    assert first.skipped_duplicate_target == 0

    cap.day = DAY2
    second = run(conn, USER_ID, cfg, [cap], today=DAY2)

    assert second.proposed == 0
    assert second.skipped_duplicate_target == 1
    proposed_events = read_all(conn, "action.proposed")
    assert len(proposed_events) == 1  # still only the DAY1 id, no DAY2 duplicate


def test_dedup_is_scoped_to_capability_and_target_not_global(conn):
    cfg = _cfg()
    cap_a = StubCapability(key="stub.a", day=DAY1, targets={"111": {"on": True}})
    cap_b = StubCapability(key="stub.b", day=DAY1, targets={"111": {"on": True}})

    report = run(conn, USER_ID, cfg, [cap_a, cap_b], today=DAY1)

    assert report.proposed == 2  # different capability -> not a duplicate
    assert report.skipped_duplicate_target == 0


# --- finding 3a: identical-hash duplicate is now visible on replay ----------


def test_duplicate_target_with_identical_inputs_appends_a_dedup_event(conn):
    """The dedup skip must not be invisible on replay -- StubCapability's
    inputs_hash is a fixed constant, so the DAY2 re-propose for the same
    target is an EXACT duplicate of the still-pending DAY1 one; skipped as
    before, but now with an `action.proposal_deduped` event naming both
    ids (planner.intent_dropped / governor refusal-is-an-event precedent)."""
    cfg = _cfg()
    cap = StubCapability(key="stub.dedup2", day=DAY1, targets={"111": {"on": True}})

    run(conn, USER_ID, cfg, [cap], today=DAY1)
    first_action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    cap.day = DAY2
    second = run(conn, USER_ID, cfg, [cap], today=DAY2)

    assert second.skipped_duplicate_target == 1
    deduped = [
        e
        for e in read_all(conn, "action.proposal_deduped")
        if e.payload["existing_action_id"] == first_action_id
    ]
    assert len(deduped) == 1
    assert deduped[0].payload["capability"] == "stub.dedup2"
    assert deduped[0].payload["target_id"] == "111"
    # unchanged: still exactly one pending proposal, no mutation of the log.
    assert len(read_all(conn, "action.proposed")) == 1
    assert read_all(conn, "action.superseded") == []


# --- finding 3b: a DIFFERENT inputs_hash supersedes the stale pending one ---


def test_duplicate_target_with_different_inputs_hash_supersedes_the_stale_pending_one(conn):
    """A pre-existing pending proposal with a STALE inputs_hash (seeded
    directly, standing in for e.g. an earlier planner-supplied seo_edit with
    worse copy) must be superseded -- not silently outrun -- by a fresh
    proposal for the SAME (capability, target_id) with a genuinely different
    inputs_hash, which is more accurate."""
    cfg = _cfg()
    cap = StubCapability(key="stub.supersede", targets={"111": {"on": True}})
    stale = cap.propose(None, USER_ID, cfg)[0]
    stale = stale.model_copy(update={"action_id": "stale-1", "inputs_hash": "stale-inputs"})
    append(conn, Event(user_id=USER_ID, type="action.proposed", payload=stale.model_dump()))

    report = run(conn, USER_ID, cfg, [cap], today=date.today())

    assert report.proposed == 1
    assert report.skipped_duplicate_target == 0
    superseded = [
        e for e in read_all(conn, "action.superseded") if e.payload["action_id"] == "stale-1"
    ]
    assert len(superseded) == 1
    new_action_id = superseded[0].payload["superseded_by"]
    assert new_action_id != "stale-1"
    assert _state_of(conn, "stale-1") == "superseded"
    assert _state_of(conn, new_action_id) == "proposed"
    # the stale id is now terminal -- unapprovable, same as any other supersede.
    blocked = approve_action(conn, USER_ID, "stale-1", [cap], cfg=cfg, today=date.today())
    assert blocked.skipped_idempotent == 1


# --- (c) supersede a sibling on execution ------------------------------------


def test_executing_one_sibling_supersedes_the_other_pending_one(conn):
    cfg = _cfg()
    cap = StubCapability(key="stub.siblings", targets={"111": {"on": True}})

    # Seed two pre-existing sibling proposals for the SAME (capability,
    # target_id) directly -- simulating duplicates that already existed
    # (e.g. from before this fix, or a planner/deterministic collision) so
    # the supersede path can be exercised independent of the dedup guard
    # above (which would otherwise prevent a second one from ever existing).
    action_a = cap.propose(None, USER_ID, cfg)[0]
    action_b = action_a.model_copy(update={"action_id": "sibling-b"})
    append(conn, Event(user_id=USER_ID, type="action.proposed", payload=action_a.model_dump()))
    append(conn, Event(user_id=USER_ID, type="action.proposed", payload=action_b.model_dump()))

    approve_action(conn, USER_ID, action_a.action_id, [cap], cfg=cfg, today=date.today())

    assert _state_of(conn, action_a.action_id) == "executed"
    assert _state_of(conn, action_b.action_id) == "superseded"
    superseded_events = read_all(conn, "action.superseded")
    assert len(superseded_events) == 1
    assert superseded_events[0].payload["action_id"] == action_b.action_id
    assert superseded_events[0].payload["superseded_by"] == action_a.action_id


def test_superseded_action_is_terminal_for_a_manual_reapprove(conn):
    cfg = _cfg()
    cap = StubCapability(key="stub.siblings2", targets={"111": {"on": True}})
    action_a = cap.propose(None, USER_ID, cfg)[0]
    action_b = action_a.model_copy(update={"action_id": "sibling-b2"})
    append(conn, Event(user_id=USER_ID, type="action.proposed", payload=action_a.model_dump()))
    append(conn, Event(user_id=USER_ID, type="action.proposed", payload=action_b.model_dump()))
    approve_action(conn, USER_ID, action_a.action_id, [cap], cfg=cfg, today=date.today())

    report = approve_action(conn, USER_ID, action_b.action_id, [cap], cfg=cfg, today=date.today())

    assert report.skipped_idempotent == 1
    assert cap.execute_calls == ["111"]  # never executed twice


# --- (b) sweep expired proposals, then re-propose still-eligible targets ----


def test_expired_pending_proposal_is_swept_terminal_and_stops_blocking_dedup(conn):
    cfg = _cfg(proposal_ttl_days=1)
    cap = StubCapability(key="stub.expiry", day=DAY1, targets={"111": {"on": True}})

    first = run(conn, USER_ID, cfg, [cap], today=DAY1)
    assert first.proposed == 1
    first_action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    # DAY1's proposal expires_at == DAY1 + 1 day == DAY2 -- still valid on
    # DAY2 itself (expires_at is inclusive: `today <= expires_at` is NOT
    # expired), so advance far enough past it to actually sweep it.
    day3 = DAY1 + timedelta(days=3)
    cap.day = day3
    second = run(conn, USER_ID, cfg, [cap], today=day3)

    assert second.expired == 1
    assert second.skipped_duplicate_target == 0  # no longer blocked -- swept first
    assert second.proposed == 1  # target still eligible -> freshly re-proposed

    assert _state_of(conn, first_action_id) == "expired"
    proposed_events = read_all(conn, "action.proposed")
    assert len(proposed_events) == 2
    assert {e.payload["action_id"] for e in proposed_events} == {
        first_action_id,
        read_all(conn, "action.proposed")[1].payload["action_id"],
    }


def test_expired_action_cannot_be_manually_reapproved(conn):
    cfg = _cfg(proposal_ttl_days=1)
    cap = StubCapability(key="stub.expiry2", day=DAY1, targets={"111": {"on": True}})
    run(conn, USER_ID, cfg, [cap], today=DAY1)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    day3 = DAY1 + timedelta(days=3)
    cap.day = day3
    run(conn, USER_ID, cfg, [cap], today=day3)  # sweeps action_id to expired

    report = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=day3)

    assert report.skipped_idempotent == 1
    assert cap.execute_calls == []
