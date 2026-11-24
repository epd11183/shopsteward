"""Brief `social_queue` + readout Buffer metrics (design 2026-11-22 §6/§12,
slice S8). Event-only fixtures."""

from datetime import date

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.projections import rebuild_ops

USER_ID = 1
AS_OF = date(2026, 6, 3)  # ISO week Mon 06-01 .. Sun 06-07
ACTION = "a" * 64


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    rebuild_core(c)
    rebuild_ops(c)
    return c


def _ev(conn, type_, **payload):
    append(conn, Event(user_id=USER_ID, type=type_, payload=payload))


def _queued(conn, post_id="bp-1", action_id=ACTION, **extra):
    _ev(
        conn,
        "social.buffer_queued",
        action_id=action_id,
        listing_id=7,
        buffer_post_id=post_id,
        channel="pinterest",
        channel_id="ch-pin",
        due_at="2026-06-04T14:00:00+00:00",
        **extra,
    )


def _observed(conn, post_id, *, status="scheduled", metrics=None, action_id=None):
    _ev(
        conn,
        "buffer.post.observed",
        post_id=post_id,
        channel="pinterest",
        status=status,
        due_at="2026-06-04T14:00:00+00:00",
        sent_at=None,
        metrics=metrics or {},
        action_id=action_id,
    )


def _brief(conn):
    return generate_brief(conn, USER_ID, ops_config.load_ops_config(), as_of=AS_OF)


def test_queued_post_shows_due_and_undo_until_and_deficit(conn):
    _queued(conn)
    _observed(conn, "bp-1", action_id=ACTION)
    brief = _brief(conn)
    (q,) = brief.social_queue.queued
    assert (q.due_at, q.undo_until) == ("2026-06-04T14:00:00+00:00",) * 2
    assert brief.social_queue.deficits["pinterest"] == 6  # 7/week, 1 scheduled this week
    text = render_text(brief)
    assert "due 2026-06-04T14:00:00+00:00, undo until 2026-06-04T14:00:00+00:00" in text
    assert "pinterest: 6 post(s) owed this week" in text


def test_sent_and_deleted_posts_leave_the_queue(conn):
    _queued(conn, "bp-1", action_id="b" * 64)
    _observed(conn, "bp-1", status="sent")
    _queued(conn, "bp-2", action_id="c" * 64)
    _ev(conn, "social.buffer_deleted", action_id="c" * 64, buffer_post_id="bp-2")
    assert _brief(conn).social_queue.queued == []


def test_unresolved_intent_is_listed_until_queued_or_lost(conn):
    intent = dict(
        action_id=ACTION,
        publish_via="buffer",
        listing_id=7,
        channel="pinterest",
        channel_id="ch-pin",
        drafted_at="2026-06-03T09:00:00+00:00",
    )
    _ev(conn, "social.pin_drafted", **intent)
    brief = _brief(conn)
    (u,) = brief.social_queue.unresolved
    assert u.listing_id == 7
    assert "UNRESOLVED" in render_text(brief)
    _ev(conn, "social.buffer_lost", action_id=ACTION, listing_id=7)
    assert _brief(conn).social_queue.unresolved == []


def test_error_status_post_is_flagged(conn):
    _observed(conn, "bp-9", status="error")
    assert "ERROR pinterest post bp-9" in render_text(_brief(conn))


def _pin_experiment(conn):
    conn.execute(
        "INSERT INTO proj_pin_experiments VALUES (?,?,?,?,?)",
        (USER_ID, 7, ACTION, "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00"),
    )
    conn.commit()


def _readout(conn):
    (r,) = analytics.pin_experiment_readout(
        conn, USER_ID, ops_config.load_ops_config(), as_of=AS_OF
    )
    return r


def test_absent_impressions_render_not_yet_reported_never_zero(conn):
    _pin_experiment(conn)
    _observed(conn, "bp-1", status="sent", metrics={"saves": 3.0}, action_id=ACTION)
    assert _readout(conn).buffer_metrics == {"saves": 3.0}
    text = render_text(_brief(conn))
    assert "impressions: not yet reported" in text
    assert "saves: 3" in text
    assert "reactions: not yet reported" in text
    assert "impressions: 0" not in text


def test_latest_observation_metrics_win_and_hand_posted_pin_has_none(conn):
    _pin_experiment(conn)
    assert _readout(conn).buffer_metrics is None  # no Buffer post: not a Buffer line
    _observed(conn, "bp-1", status="sent", metrics={"impressions": 10.0}, action_id=ACTION)
    _observed(conn, "bp-1", status="sent", metrics={"impressions": 25.0}, action_id=ACTION)
    assert _readout(conn).buffer_metrics == {"impressions": 25.0}
