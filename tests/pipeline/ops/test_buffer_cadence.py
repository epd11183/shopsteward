"""Buffer cadence (design 2026-11-22 §4/§7, slice S7): governor PACE,
`cadence_deficit`, per-run overrides. Event-only fixtures."""

from datetime import UTC, date, datetime, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.ops import cadence
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.governor import govern
from shopsteward.pipeline.ops.models import ProposedAction, RefusalReason, Tier
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
TODAY = date(2026, 6, 3)  # a Wednesday; ISO week Mon 06-01 .. Sun 06-07


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _cfg(per_week: int = 4):
    cfg = ops_config.load_ops_config()
    cfg.autonomy.enabled = True
    cfg.buffer.channels["pinterest"].posts_per_week = per_week
    return cfg


def _observe(
    conn, post_id, *, channel="pinterest", status="sent", day="2026-06-02", action_id=None
):
    when = f"{day}T14:00:00+00:00"
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="buffer.post.observed",
            payload={
                "post_id": post_id,
                "channel": channel,
                "status": status,
                "due_at": when,
                "sent_at": when if status == "sent" else None,
                "metrics": {},
                "action_id": action_id,
            },
        ),
    )


def _queue(conn, post_id, *, day="2026-06-04"):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="social.buffer_queued",
            payload={
                "action_id": f"act-{post_id}",
                "listing_id": 1,
                "buffer_post_id": post_id,
                "channel": "pinterest",
                "channel_id": "ch-pin",
                "due_at": f"{day}T14:00:00+00:00",
            },
        ),
    )


def _action(**params) -> ProposedAction:
    return ProposedAction(
        action_id="a-1",
        capability="stub.noop",
        target_type="stub",
        target_id="1",
        tier=Tier.NOTIFY,
        reason="r.",
        inputs_hash="h",
        undo_available=True,
        expires_at=(TODAY + timedelta(days=1)).isoformat(),
        params=params,
    )


def _govern(conn, cfg, **params):
    return govern(conn, USER_ID, _action(**params), StubCapability(), cfg, TODAY)


def test_three_observed_two_foreign_plus_one_unobserved_queued_is_pace(conn):
    _observe(conn, "p1", action_id="mine")
    _observe(conn, "p2")  # foreign (Desktop job)
    _observe(conn, "p3", status="scheduled", day="2026-06-05")  # foreign
    _queue(conn, "p4")
    decision = _govern(conn, _cfg(4), publish_via="buffer")
    assert decision.reason == RefusalReason.PACE
    assert cadence.cadence_deficit(conn, USER_ID, _cfg(4), TODAY)["pinterest"] == 0


def test_queued_that_is_also_observed_is_not_double_counted(conn):
    for i in (1, 2, 3):
        _observe(conn, f"p{i}")
    _queue(conn, "p3")  # same post, already observed
    assert _govern(conn, _cfg(4), publish_via="buffer").approved is True
    assert cadence.cadence_deficit(conn, USER_ID, _cfg(4), TODAY)["pinterest"] == 1


def test_latest_observation_wins_and_errors_do_not_count(conn):
    _observe(conn, "p1", status="scheduled")
    _observe(conn, "p1", status="error")
    assert cadence.cadence_deficit(conn, USER_ID, _cfg(4), TODAY)["pinterest"] == 4


def test_other_week_other_channel_and_deleted_posts_do_not_count(conn):
    _observe(conn, "p1", day="2026-05-28")  # last week
    _observe(conn, "p2", channel="instagram")
    _queue(conn, "p3")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="social.buffer_deleted",
            payload={"action_id": "act-p3", "buffer_post_id": "p3"},
        ),
    )
    assert cadence.cadence_deficit(conn, USER_ID, _cfg(4), TODAY)["pinterest"] == 4


def test_pace_applies_only_to_buffer_publishes(conn):
    for i in range(4):
        _observe(conn, f"p{i}")
    assert _govern(conn, _cfg(4)).approved is True  # draft-only path: untouched


def test_queue_depth_full_is_pace(conn):
    cfg = _cfg(99)
    cfg.buffer.channels["pinterest"].max_queue_depth = 2
    _observe(conn, "p1", status="scheduled", day="2026-06-09")  # next week, still queued
    _queue(conn, "p2", day="2026-06-10")
    assert _govern(conn, cfg, publish_via="buffer").reason == RefusalReason.PACE


def test_per_run_override_replaces_the_default_cap_for_that_capability_only(conn):
    from shopsteward.adapters.planner.fake import FakePlannerAdapter
    from shopsteward.adapters.planner.interface import ProposalIntent
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.ops.planner import plan_proposals
    from shopsteward.pipeline.ops.projections import rebuild_ops

    cfg = _cfg()
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg.autonomy.planner_max_per_capability_per_run = 1
    cfg.autonomy.planner_max_per_run_overrides = {"stub.cap": 2}
    cap = StubCapability(key="stub.cap", targets={f"t-{i}": {"on": True} for i in (1, 2, 3)})
    adapter = FakePlannerAdapter(
        plan=[
            ProposalIntent(capability_key="stub.cap", target_id=f"t-{i}", reason="r")
            for i in (1, 2, 3)
        ]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert [p.target_id for p in proposals] == ["t-1", "t-2"]


def test_facts_json_carries_cadence_deficit_per_channel(conn):
    import json

    from shopsteward.adapters.planner.fake import FakePlannerAdapter
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.ops.planner import plan_proposals
    from shopsteward.pipeline.ops.projections import rebuild_ops

    cfg = _cfg()
    rebuild_core(conn)
    rebuild_ops(conn)
    _observe(conn, "p1")
    adapter = FakePlannerAdapter(plan=[])

    plan_proposals(
        conn, USER_ID, cfg, adapter, [], soft_cap_usd=10.0, as_of=datetime(2026, 6, 3, tzinfo=UTC)
    )

    deficit = json.loads(adapter.plan_calls[0])["cadence_deficit"]
    assert deficit["pinterest"] == cfg.buffer.channels["pinterest"].posts_per_week - 1
    assert deficit["instagram"] == cfg.buffer.channels["instagram"].posts_per_week


def test_per_run_overrides_ship_in_defaults():
    cfg = ops_config.load_ops_config()
    assert cfg.autonomy.planner_max_per_run_overrides == {
        "social.caption_draft": 2,
        "social.pinterest_post": 2,
    }
