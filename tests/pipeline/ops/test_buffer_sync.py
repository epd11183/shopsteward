"""`ops buffer-sync` (design 2026-11-22 §6, slice S4). Fakes/fixtures only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shopsteward.adapters.buffer.fake import FakeBufferAdapter
from shopsteward.adapters.buffer.live import _parse_post
from shopsteward.adapters.buffer.models import BufferChannel, BufferPost
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops.buffer_sync import sync_buffer
from shopsteward.pipeline.ops.registry import REGISTRY
from tests.pipeline.ops.test_mark_posted import LISTING_ID, USER_ID, _draft_a_real_pin

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "buffer"


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _adapter() -> FakeBufferAdapter:
    return FakeBufferAdapter(
        [
            BufferChannel(channel_id="ch-pin", service="pinterest", name="p"),
            BufferChannel(channel_id="ch-ig", service="instagram", name="i"),
        ]
    )


def _pin_url(action_id: str) -> str:
    return f"https://shop.example/l?utm_source=pinterest&utm_content={action_id[:12]}"


def _sent(adapter: FakeBufferAdapter, action_id: str, sent_at: datetime) -> None:
    adapter.posts["p1"] = BufferPost(
        post_id="p1",
        channel_id="ch-pin",
        status="sent",
        pin_url=_pin_url(action_id),
        sent_at=sent_at,
        created_at=sent_at - timedelta(hours=1),
        metrics={"impressions": 10.0},
    )


def test_sent_post_marks_pin_posted_once_at_sent_at_and_resync_appends_nothing(conn):
    action_id = _draft_a_real_pin(conn, LISTING_ID)
    adapter = _adapter()
    sent_at = datetime.now(UTC)  # after the draft, not in the future
    _sent(adapter, action_id, sent_at)

    first = sync_buffer(conn, USER_ID, adapter)
    assert first.marked_posted == 1
    posted = read_all(conn, "social.pin_posted")
    assert len(posted) == 1
    assert datetime.fromisoformat(posted[0].payload["posted_at"]) == sent_at
    assert posted[0].payload["action_id"] == action_id

    n = len(read_all(conn))
    again = sync_buffer(conn, USER_ID, adapter)
    assert (again.observed_new, again.marked_posted) == (0, 0)
    assert len(read_all(conn)) == n


def test_metrics_change_appends_one_observed_event_and_absent_is_not_zero(conn):
    adapter = _adapter()
    adapter.posts["p1"] = BufferPost(
        post_id="p1", channel_id="ch-ig", status="sent", sent_at=datetime.now(UTC)
    )
    sync_buffer(conn, USER_ID, adapter)
    assert read_all(conn, "buffer.post.observed")[0].payload["metrics"] == {}
    adapter.posts["p1"].metrics = {"impressions": 3.0}
    assert sync_buffer(conn, USER_ID, adapter).observed_new == 1
    assert sync_buffer(conn, USER_ID, adapter).observed_new == 0
    assert len(read_all(conn, "buffer.post.observed")) == 2


def test_foreign_post_is_recorded_unmatched_and_never_marked_posted(conn):
    adapter = _adapter()
    adapter.posts["p1"] = BufferPost(
        post_id="p1", channel_id="ch-ig", status="sent", text="hello", sent_at=datetime.now(UTC)
    )
    res = sync_buffer(conn, USER_ID, adapter)
    assert (res.unmatched, res.observed_new, res.marked_posted) == (1, 1, 0)
    assert res.seen == {"instagram/sent": 1}
    assert read_all(conn, "buffer.post.observed")[0].payload["action_id"] is None
    assert read_all(conn, "social.pin_posted") == []


def test_scheduled_mapped_post_is_observed_but_not_marked_posted(conn):
    action_id = _draft_a_real_pin(conn, LISTING_ID)
    adapter = _adapter()
    _sent(adapter, action_id, datetime.now(UTC))
    adapter.posts["p1"].status = "scheduled"
    adapter.posts["p1"].sent_at = None
    res = sync_buffer(conn, USER_ID, adapter)
    assert (res.observed_new, res.marked_posted, res.unmatched) == (1, 0, 0)


def test_recorded_fixtures_parse_through_the_sync_path(conn):
    raw = json.loads((FIXTURES / "recorded_posts.json").read_text())
    nodes = [e["node"] for e in raw["data"]["posts"]["edges"]]
    adapter = FakeBufferAdapter(
        [
            BufferChannel(channel_id=n["channelId"], service="pinterest", name="x")
            for n in {n["channelId"]: n for n in nodes}.values()
        ]
    )
    for n in nodes:
        p = _parse_post(n)
        adapter.posts[p.post_id] = p
    res = sync_buffer(conn, USER_ID, adapter, since_days=3650)
    assert sum(res.seen.values()) == len(nodes) > 0
    assert res.unmatched == len(nodes) and res.marked_posted == 0
    assert sync_buffer(conn, USER_ID, adapter, since_days=3650).observed_new == 0


def test_cli_live_refuses_when_gate_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_BUFFER", raising=False)
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    result = runner.invoke(app, ["ops", "buffer-sync", "--live"])
    assert result.exit_code == 1
    assert "SHOPSTEWARD_LIVE_BUFFER" in result.output


def test_cli_offline_default_runs_on_the_fake(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    result = runner.invoke(app, ["ops", "buffer-sync"])
    assert result.exit_code == 0, result.output
    assert "newly recorded: 0 observed, 0 marked posted" in result.output
