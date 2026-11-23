from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.buffer.fake import FakeBufferAdapter
from shopsteward.adapters.buffer.interface import BufferError, BufferUnknownOutcome
from shopsteward.adapters.buffer.models import BufferChannel, BufferPostSpec

SINCE = datetime.now(UTC) - timedelta(days=1)


def _fake() -> FakeBufferAdapter:
    return FakeBufferAdapter([BufferChannel(channel_id="ch-1", service="pinterest", name="n")])


def test_create_list_delete_and_second_delete_raises() -> None:
    f = _fake()
    assert [c.service for c in f.list_channels()] == ["pinterest"]
    post = f.create_post(BufferPostSpec(channel_id="ch-1", text="hi"))
    assert post.post_id == "post-1" and post.status == "scheduled"
    listed = f.list_posts(channel_ids=["ch-1"], created_since=SINCE)
    assert [p.post_id for p in listed] == ["post-1"]
    assert f.list_posts(channel_ids=["ch-2"], created_since=SINCE) == []
    f.delete_post("post-1")
    assert f.list_posts(channel_ids=["ch-1"], created_since=SINCE) == []
    with pytest.raises(BufferError) as exc:
        f.delete_post("post-1")
    assert exc.value.code == "NOT_FOUND"


def test_post_without_metrics_has_empty_metrics() -> None:
    f = _fake()
    f.create_post(BufferPostSpec(channel_id="ch-1", text="hi"))
    (p,) = f.list_posts(channel_ids=["ch-1"], created_since=SINCE, with_metrics=True)
    assert p.metrics == {}


def test_fail_after_create_once_leaves_post_findable() -> None:
    f = _fake()
    f.fail_after_create_once = True
    with pytest.raises(BufferUnknownOutcome):
        f.create_post(BufferPostSpec(channel_id="ch-1", text="hi"))
    assert f.create_count == 1
    assert len(f.list_posts(channel_ids=["ch-1"], created_since=SINCE)) == 1
    f.create_post(BufferPostSpec(channel_id="ch-1", text="again"))  # flag is one-shot
