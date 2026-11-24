"""`social.pinterest_post` Variant B + `buffer_publish` (design
2026-11-22 §5, slice S5). Fakes only -- never a live Buffer call."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.buffer.fake import FakeBufferAdapter
from shopsteward.adapters.buffer.models import BufferChannel
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import buffer_publish as bp
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.buffer_sync import sync_buffer
from shopsteward.pipeline.ops.capabilities.pinterest_post import SocialPinterestPost, _candidates
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
LISTING = 801
BOARD = "Nature & Landscape Wall Art"


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _image(w: int = 3000, h: int = 2000) -> dict:
    return {
        "listing_image_id": 1,
        "rank": 1,
        "url_570xN": "https://x/570.jpg",
        "url_fullxfull": "https://x/full.jpg",
        "full_width": w,
        "full_height": h,
    }


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    seed_listing_observed_on(
        c, listing_id=LISTING, title="Cranes", day=datetime.now(UTC).date(), views=10
    )
    append(
        c,
        Event(
            user_id=USER_ID,
            type="etsy.listing.images.observed",
            payload={"listing_id": LISTING, "images": [_image()]},
        ),
    )
    ops_config.seed(c, USER_ID)
    rebuild_core(c)
    rebuild_ops(c)
    return c


@pytest.fixture()
def fake() -> FakeBufferAdapter:
    return FakeBufferAdapter(
        [BufferChannel(channel_id="ch-pin", service="pinterest", name="p", boards={BOARD: "b1"})]
    )


def _cap_and_action(conn, fake):
    cap = SocialPinterestPost(fake)
    action = cap.materialize(
        conn,
        USER_ID,
        ops_config.get_ops_config(conn, USER_ID),
        ProposalIntent(
            capability_key="social.pinterest_post",
            target_id=str(LISTING),
            params={
                "title": "Cranes",
                "description": "A print.",
                "alt_text": "Cranes at dawn",
                "board_key": "wall_art",
            },
            reason="r",
        ),
    )
    assert action is not None
    return cap, action


def _types(conn, t):
    return [e for e in read_all(conn, t) if e.user_id == USER_ID]


def test_tier_undo_and_variant_a_unchanged(fake):
    a = SocialPinterestPost()
    assert (a.max_tier, a.undo, a.policy_verified) == (Tier.PROPOSE, None, True)
    b = SocialPinterestPost(fake)
    assert b.max_tier == Tier.NOTIFY and callable(b.undo)
    register(b)  # invariant 1: NOTIFY needs a callable undo
    assert b.estimate_cost_usd(None) == 0.0  # type: ignore[arg-type]


def test_execute_queues_one_scheduled_post_no_ai_label(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    assert action.params["publish_via"] == "buffer"
    res = cap.execute(conn, USER_ID, action)
    assert fake.create_count == 1 and res.after["buffer_post_id"] == "post-1"
    q = _types(conn, bp.QUEUED)[0].payload
    assert q["ai_label_applied"] is False and q["reconciled"] is False
    post = fake.posts["post-1"]
    assert post.status == "scheduled" and post.pin_url.endswith(action.action_id[:12])
    assert post.due_at >= datetime.now(UTC) + timedelta(hours=3, minutes=59)
    # rerun returns the recorded result, never a second create
    assert cap.execute(conn, USER_ID, action).after == res.after
    assert fake.create_count == 1


def test_timeout_with_post_findable_creates_once(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    fake.fail_after_create_once = True
    cap.execute(conn, USER_ID, action)
    assert fake.create_count == 1
    assert len(_types(conn, bp.QUEUED)) == 1


def test_timeout_post_not_findable_blocks_pair_then_sync_reconciles(conn, fake, monkeypatch):
    cap, action = _cap_and_action(conn, fake)
    real_list = fake.list_posts
    monkeypatch.setattr(fake, "list_posts", lambda **_: [])
    fake.fail_after_create_once = True
    with pytest.raises(ValueError, match="unresolved"):
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 1 and not _types(conn, bp.QUEUED)
    assert LISTING in bp.blocked_listings(conn, USER_ID)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    far = datetime.now(UTC) + timedelta(days=30)  # cooldown long over
    assert str(LISTING) not in _candidates(conn, USER_ID, cfg, now=far, publishing=True)
    with pytest.raises(ValueError):  # retry: still refused, no second create
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 1

    monkeypatch.setattr(fake, "list_posts", real_list)
    res = sync_buffer(conn, USER_ID, fake)
    assert res.reconciled == 1
    q = _types(conn, bp.QUEUED)
    assert len(q) == 1 and q[0].payload["reconciled"] is True
    assert fake.create_count == 1 and not bp.blocked_listings(conn, USER_ID)
    assert sync_buffer(conn, USER_ID, fake).reconciled == 0  # re-sync appends nothing new
    assert len(_types(conn, bp.QUEUED)) == 1


def test_sync_maps_sent_post_via_queued_and_marks_posted(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    cap.execute(conn, USER_ID, action)
    fake.posts["post-1"] = fake.posts["post-1"].model_copy(
        update={"status": "sent", "sent_at": datetime.now(UTC)}
    )
    res = sync_buffer(conn, USER_ID, fake)
    assert res.marked_posted == 1 and res.unmatched == 0
    assert len(_types(conn, "social.pin_posted")) == 1


def test_lost_only_for_a_channel_listed_this_run(conn, fake, monkeypatch):
    cap, action = _cap_and_action(conn, fake)
    monkeypatch.setattr(fake, "list_posts", lambda **_: [])
    fake.fail_after_create_once = True
    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, action)
    monkeypatch.undo()
    late = datetime.now(UTC) + timedelta(hours=2)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert bp.reconcile(conn, USER_ID, fake, cfg, [], now=late) == 0  # channel not listed
    assert bp.blocked_listings(conn, USER_ID)
    chans = fake.list_channels()
    assert bp.reconcile(conn, USER_ID, fake, cfg, chans, now=late) == 1  # post exists: queued
    assert not bp.blocked_listings(conn, USER_ID)


def test_zero_matches_past_window_releases_pair(conn, fake, monkeypatch):
    cap, action = _cap_and_action(conn, fake)
    monkeypatch.setattr(fake, "list_posts", lambda **_: [])
    fake.fail_after_create_once = True
    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, action)
    fake.posts.clear()
    cfg = ops_config.get_ops_config(conn, USER_ID)
    soon = datetime.now(UTC) + timedelta(minutes=5)
    assert bp.reconcile(conn, USER_ID, fake, cfg, fake.list_channels(), now=soon) == 0
    monkeypatch.undo()
    late = datetime.now(UTC) + timedelta(hours=2)
    assert bp.reconcile(conn, USER_ID, fake, cfg, fake.list_channels(), now=late) == 1
    assert len(_types(conn, bp.LOST)) == 1 and not bp.blocked_listings(conn, USER_ID)


def test_publish_intent_without_publisher_refuses(conn, fake):
    _, action = _cap_and_action(conn, fake)
    with pytest.raises(ValueError, match="publish_via=buffer"):
        SocialPinterestPost().execute(conn, USER_ID, action)
    assert not _types(conn, "social.pin_drafted")


def test_small_or_unknown_image_dropped_only_when_publishing(conn):
    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert str(LISTING) in _candidates(conn, USER_ID, cfg, publishing=True)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.images.observed",
            payload={"listing_id": LISTING, "images": [_image(600, 400)]},
        ),
    )
    assert str(LISTING) not in _candidates(conn, USER_ID, cfg, publishing=True)
    assert str(LISTING) in _candidates(conn, USER_ID, cfg)  # Variant A unchanged


def test_refusals_before_intent_leave_no_marker(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    fake.channels[0].is_queue_paused = True
    with pytest.raises(ValueError, match="paused"):
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 0 and not bp.blocked_listings(conn, USER_ID)


def test_undo_deletes_while_queued_but_not_after_send(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    cap.execute(conn, USER_ID, action)
    fake.posts["post-1"] = fake.posts["post-1"].model_copy(update={"status": "sent"})
    with pytest.raises(ValueError, match="remove it on Pinterest"):
        cap.undo(conn, USER_ID, action)
    assert "post-1" in fake.posts and not _types(conn, bp.DELETED)
    fake.posts["post-1"] = fake.posts["post-1"].model_copy(update={"status": "scheduled"})
    cap.undo(conn, USER_ID, action)
    assert "post-1" not in fake.posts and len(_types(conn, bp.DELETED)) == 1


def test_brief_hides_buffer_drafts_from_hand_post_queue(conn, fake):
    from shopsteward.pipeline.ops.brief import _pin_drafts

    cap, action = _cap_and_action(conn, fake)
    cap.execute(conn, USER_ID, action)
    assert _pin_drafts(conn, USER_ID, datetime.now(UTC).date()) == []


def test_runner_refuse_reconcile_reapprove_then_undo(conn, fake, monkeypatch):
    from shopsteward.pipeline.ops.runner import approve_action, run, undo_action

    cap, action = _cap_and_action(conn, fake)
    register(cap)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    today = datetime.now(UTC).date()
    run(conn, USER_ID, cfg, [cap], today=today, proposals=[action])
    real_list = fake.list_posts
    monkeypatch.setattr(fake, "list_posts", lambda **_: [])
    fake.fail_after_create_once = True
    rep = approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, today=today)
    assert rep.refused == 1 and not _types(conn, "action.failed")
    assert _types(conn, "action.refused") and fake.create_count == 1

    monkeypatch.setattr(fake, "list_posts", real_list)
    sync_buffer(conn, USER_ID, fake)
    rep = approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, today=today)
    assert rep.executed == 1 and fake.create_count == 1
    assert len(_types(conn, "action.executed")) == 1

    undo_action(conn, USER_ID, action.action_id, [cap])
    assert len(_types(conn, bp.DELETED)) == 1 and "post-1" not in fake.posts


def test_read_gate_alone_never_builds_a_publisher(monkeypatch):
    """buffer-sync runs live with SHOPSTEWARD_LIVE_BUFFER=1; posting needs the
    separate WRITE gate too (stays off until the S9 cutover)."""
    from shopsteward.pipeline.ops.cli import _build_buffer_publisher

    monkeypatch.setenv("SHOPSTEWARD_LIVE_BUFFER", "1")
    monkeypatch.setenv("BUFFER_API_KEY", "fx-key")
    monkeypatch.delenv("SHOPSTEWARD_LIVE_BUFFER_WRITE", raising=False)
    assert _build_buffer_publisher(True) is None
    monkeypatch.setenv("SHOPSTEWARD_LIVE_BUFFER_WRITE", "1")
    assert _build_buffer_publisher(False) is None
    assert _build_buffer_publisher(True) is not None
