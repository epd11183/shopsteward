"""`social.caption_draft` Variant B (design 2026-11-22 §4/§5, slice S6).
Fakes only -- never a live Buffer call."""

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
from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft, _candidates
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
LISTING = 801
IG = f"{LISTING}:instagram"
FB = f"{LISTING}:facebook"


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


def _set_image(conn, w: int, h: int) -> None:
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.images.observed",
            payload={"listing_id": LISTING, "images": [_image(w, h)]},
        ),
    )


def _cfg(conn):
    cfg = ops_config.get_ops_config(conn, USER_ID)
    for ch in cfg.caption.channels.values():
        ch.eligibility = "explore"  # coverage-first: no sales fixture needed
    return cfg


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    seed_listing_observed_on(
        c, listing_id=LISTING, title="Cranes", day=datetime.now(UTC).date(), views=10
    )
    _set_image(c, 3000, 2000)
    ops_config.seed(c, USER_ID)
    rebuild_core(c)
    rebuild_ops(c)
    return c


@pytest.fixture()
def fake() -> FakeBufferAdapter:
    return FakeBufferAdapter(
        [
            BufferChannel(channel_id="ch-ig", service="instagram", name="i"),
            BufferChannel(channel_id="ch-fb", service="facebook", name="f"),
        ]
    )


def _cap_and_action(conn, fake, target_id=IG, caption="Cranes at dawn."):
    cap = SocialCaptionDraft(fake)
    action = cap.materialize(
        conn,
        USER_ID,
        _cfg(conn),
        ProposalIntent(
            capability_key="social.caption_draft",
            target_id=target_id,
            params={"caption": caption},
            reason="r",
        ),
    )
    assert action is not None
    return cap, action


def _types(conn, t):
    return [e for e in read_all(conn, t) if e.user_id == USER_ID]


def test_publisher_none_is_variant_a_unchanged(conn, fake):
    a = SocialCaptionDraft()
    assert (a.max_tier, a.undo, a.policy_verified) == (Tier.PROPOSE, None, True)
    b = SocialCaptionDraft(fake)
    assert b.max_tier == Tier.NOTIFY and callable(b.undo)
    register(b)  # invariant 1: NOTIFY needs a callable undo
    action = a.materialize(
        conn,
        USER_ID,
        _cfg(conn),
        ProposalIntent(
            capability_key="social.caption_draft",
            target_id=IG,
            params={"caption": "Hi"},
            reason="r",
        ),
    )
    assert action is not None and "publish_via" not in action.params and not action.undo_available
    a.execute(conn, USER_ID, action)
    drafted = _types(conn, "social.caption_drafted")[0].payload
    assert set(drafted) == {"listing_id", "channel", "caption", "title", "action_id", "drafted_at"}
    assert fake.create_count == 0


def test_ig_aspect_066_is_never_a_candidate_but_fb_is(conn):
    cfg = _cfg(conn)
    _set_image(conn, 2000, 3030)  # aspect 0.66, outside [0.8, 1.91]
    got = _candidates(conn, USER_ID, cfg, publishing=True)
    assert IG not in got and FB in got
    assert IG in _candidates(conn, USER_ID, cfg)  # Variant A unchanged
    _set_image(conn, 3000, 2000)
    assert IG in _candidates(conn, USER_ID, cfg, publishing=True)


def test_ig_aspect_066_refused_at_execute_with_no_marker(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    _set_image(conn, 2000, 3030)
    with pytest.raises(ValueError, match="no publishable image"):
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 0 and not bp.blocked_captions(conn, USER_ID)


def test_materialize_drops_ungroundable_ig_aspect(conn, fake):
    _set_image(conn, 2000, 3030)
    cap = SocialCaptionDraft(fake)
    intent = ProposalIntent(
        capability_key="social.caption_draft", target_id=IG, params={"caption": "x"}, reason="r"
    )
    assert cap.materialize(conn, USER_ID, _cfg(conn), intent) is None


def test_fb_text_ends_in_utm_url_and_due_at_respects_lead(conn, fake):
    cap, action = _cap_and_action(conn, fake, FB)
    res = cap.execute(conn, USER_ID, action)
    assert fake.create_count == 1 and res.after["channel"] == "facebook"
    post = fake.posts["post-1"]
    assert post.text.startswith("Cranes at dawn.\n\n")
    assert post.text.endswith(f"utm_content={action.action_id[:12]}")
    assert "utm_source=facebook" in post.text
    cfg = ops_config.get_ops_config(conn, USER_ID)
    lead = timedelta(hours=cfg.buffer.min_lead_hours)
    assert post.due_at >= datetime.now(UTC) + lead - timedelta(seconds=5)
    q = _types(conn, bp.QUEUED)[0].payload
    assert q["kind"] == "caption" and q["ai_label_applied"] is False


def test_ig_text_is_the_bare_caption_and_rerun_never_recreates(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    res = cap.execute(conn, USER_ID, action)
    assert fake.posts["post-1"].text == "Cranes at dawn."
    assert cap.execute(conn, USER_ID, action).after == res.after
    assert fake.create_count == 1


def test_timeout_with_post_findable_creates_once(conn, fake):
    for target in (IG, FB):
        cap, action = _cap_and_action(conn, fake, target)
        fake.fail_after_create_once = True
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 2 and len(_types(conn, bp.QUEUED)) == 2


@pytest.mark.parametrize("target", [IG, FB])
def test_timeout_not_findable_blocks_pair_then_sync_reconciles(conn, fake, monkeypatch, target):
    cap, action = _cap_and_action(conn, fake, target)
    real_list = fake.list_posts
    monkeypatch.setattr(fake, "list_posts", lambda **_: [])
    fake.fail_after_create_once = True
    with pytest.raises(ValueError, match="unresolved"):
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 1 and not _types(conn, bp.QUEUED)
    channel = target.split(":")[1]
    assert (LISTING, channel) in bp.blocked_captions(conn, USER_ID)
    cfg = _cfg(conn)
    far = datetime.now(UTC) + timedelta(days=30)  # cooldown long over
    assert target not in _candidates(conn, USER_ID, cfg, now=far, publishing=True)
    with pytest.raises(ValueError):  # retry: still refused, no second create
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 1

    monkeypatch.setattr(fake, "list_posts", real_list)
    assert sync_buffer(conn, USER_ID, fake).reconciled == 1
    q = _types(conn, bp.QUEUED)
    assert len(q) == 1 and q[0].payload["reconciled"] is True and q[0].payload["kind"] == "caption"
    assert fake.create_count == 1 and not bp.blocked_captions(conn, USER_ID)
    assert sync_buffer(conn, USER_ID, fake).reconciled == 0
    assert len(_types(conn, bp.QUEUED)) == 1


def test_blocked_ig_caption_does_not_block_the_fb_caption_or_a_pin(conn, fake, monkeypatch):
    cap, action = _cap_and_action(conn, fake, IG)
    monkeypatch.setattr(fake, "list_posts", lambda **_: [])
    fake.fail_after_create_once = True
    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, action)
    far = datetime.now(UTC) + timedelta(days=30)
    got = _candidates(conn, USER_ID, _cfg(conn), now=far, publishing=True)
    assert IG not in got and FB in got
    assert not bp.blocked_listings(conn, USER_ID)  # pins unaffected


def test_sync_maps_sent_caption_via_queued_and_marks_posted(conn, fake):
    cap, action = _cap_and_action(conn, fake, FB)
    cap.execute(conn, USER_ID, action)
    fake.posts["post-1"] = fake.posts["post-1"].model_copy(
        update={"status": "sent", "sent_at": datetime.now(UTC)}
    )
    res = sync_buffer(conn, USER_ID, fake)
    assert res.marked_posted == 1 and res.unmatched == 0
    posted = _types(conn, "social.caption_posted")
    assert len(posted) == 1 and posted[0].payload["action_id"] == action.action_id


def test_publish_intent_without_publisher_refuses(conn, fake):
    _, action = _cap_and_action(conn, fake)
    with pytest.raises(ValueError, match="publish_via=buffer"):
        SocialCaptionDraft().execute(conn, USER_ID, action)
    assert not _types(conn, "social.caption_drafted")


def test_refusal_before_intent_leaves_no_marker(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    fake.channels[0].is_queue_paused = True
    with pytest.raises(ValueError, match="paused"):
        cap.execute(conn, USER_ID, action)
    assert fake.create_count == 0 and not bp.blocked_captions(conn, USER_ID)


def test_undo_deletes_while_queued_but_not_after_send(conn, fake):
    cap, action = _cap_and_action(conn, fake)
    cap.execute(conn, USER_ID, action)
    fake.posts["post-1"] = fake.posts["post-1"].model_copy(update={"status": "sent"})
    with pytest.raises(ValueError, match="remove it on instagram"):
        cap.undo(conn, USER_ID, action)
    assert "post-1" in fake.posts and not _types(conn, bp.DELETED)
    fake.posts["post-1"] = fake.posts["post-1"].model_copy(update={"status": "scheduled"})
    cap.undo(conn, USER_ID, action)
    assert "post-1" not in fake.posts and len(_types(conn, bp.DELETED)) == 1


def test_brief_hides_buffer_drafts_from_hand_post_queue(conn, fake):
    from shopsteward.pipeline.ops.brief import _caption_drafts

    cap, action = _cap_and_action(conn, fake)
    cap.execute(conn, USER_ID, action)
    assert _caption_drafts(conn, USER_ID, datetime.now(UTC).date()) == []


def test_runner_refuse_reconcile_reapprove_then_undo(conn, fake, monkeypatch):
    from shopsteward.pipeline.ops.runner import approve_action, run, undo_action

    cap, action = _cap_and_action(conn, fake)
    register(cap)
    cfg = _cfg(conn)
    cfg.autonomy.tier_floor = {
        k: v for k, v in cfg.autonomy.tier_floor.items() if not k.startswith("social.")
    }  # exercise the PROPOSE -> approve path
    today = datetime.now(UTC).date()
    run(conn, USER_ID, cfg, [cap], today=today, proposals=[action])
    real_list = fake.list_posts
    monkeypatch.setattr(fake, "list_posts", lambda **_: [])
    fake.fail_after_create_once = True
    rep = approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, today=today)
    assert rep.refused == 1 and not _types(conn, "action.failed")
    assert fake.create_count == 1

    monkeypatch.setattr(fake, "list_posts", real_list)
    sync_buffer(conn, USER_ID, fake)
    # Its own drafted event is inside the cooldown, yet re-approval must succeed.
    rep = approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, today=today)
    assert rep.executed == 1 and fake.create_count == 1

    undo_action(conn, USER_ID, action.action_id, [cap])
    assert len(_types(conn, bp.DELETED)) == 1 and "post-1" not in fake.posts
