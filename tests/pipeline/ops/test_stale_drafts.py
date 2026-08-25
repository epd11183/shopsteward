"""E9 (2026-08-25): `analytics.stale_drafts()` -- drafted pins/captions
older than `cfg.social.staleness_days` with no corresponding `*_posted`
event, surfaced READ-TIME against an injected `as_of` (never a written
event -- see analytics.py's module docstring/replay-determinism rule).
Complementary to the existing caption cooldown COUNT in
`data_quality_notes()`: this is the "these specific drafts are aging
unposted" view.

Drafts are placed in the past by setting the event PAYLOAD's own
`drafted_at`/`action_id` fields directly (`stale_drafts()`, like
`pin_experiment_readout()`, always prefers the payload value over the
event's real append-time `created_at`) -- no raw-SQL timestamp override
needed (test_pin_experiments.py's `_seed_pin_action` precedent, simplified
since this module never needs the DB row's own created_at to be old)."""

from datetime import date, timedelta

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.caption_draft import mark_posted as mark_caption_posted
from shopsteward.pipeline.ops.capabilities.pinterest_post import mark_posted as mark_pin_posted
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
AS_OF = date(2026, 6, 1)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_pin(conn, listing_id: int, action_id: str, drafted_day: date) -> None:
    """Seeds the (action.proposed, action.executed, social.pin_drafted)
    triple `proj_pin_experiments` needs -- action_id must be a full 64-char
    id (`_resolve_pin_drafted`'s own requirement, `mark_posted()` reuses
    it)."""
    seed_listing_observed_on(
        conn, listing_id=listing_id, title="Stale Pin Print", day=AS_OF, views=10
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="action.proposed",
            payload={
                "action_id": action_id,
                "capability": "social.pinterest_post",
                "target_type": "listing",
                "target_id": str(listing_id),
                "tier": 2,
                "reason": "test-seeded",
                "inputs_hash": "deadbeef",
                "estimated_cost_usd": 0.0,
                "undo_available": False,
                "expires_at": (drafted_day + timedelta(days=14)).isoformat(),
                "params": {},
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="social.pin_drafted",
            payload={
                "listing_id": listing_id,
                "title": "x",
                "description": "x",
                "alt_text": "x",
                "board_key": "wall_art",
                "destination_url": (
                    f"https://www.etsy.com/listing/{listing_id}?utm_content={action_id[:12]}"
                ),
                "image_url": "https://example.com/img.jpg",
                "drafted_at": drafted_day.isoformat(),
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="action.executed",
            payload={
                "action_id": action_id,
                "before": {},
                "after": {"listing_id": listing_id, "board_key": "wall_art"},
                "cost_usd": 0.0,
                "duration_ms": 0,
            },
        ),
    )


def _seed_caption(
    conn, listing_id: int, action_id: str, drafted_day: date, channel="instagram"
) -> None:
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="social.caption_drafted",
            payload={
                "listing_id": listing_id,
                "channel": channel,
                "caption": "Buy this print!",
                "title": "Stale Caption Print",
                "action_id": action_id,
                "drafted_at": drafted_day.isoformat(),
            },
        ),
    )


def test_a_stale_unposted_pin_is_surfaced(tmp_path):
    conn = _conn(tmp_path)
    old_day = AS_OF - timedelta(days=20)  # older than the default 14d threshold
    action_id = "1" * 64
    _seed_pin(conn, 951, action_id, old_day)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    stale = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)

    assert len(stale) == 1
    assert stale[0].channel == "pin"
    assert stale[0].listing_id == 951
    assert stale[0].action_id == action_id
    assert stale[0].days_stale == 20


def test_a_posted_pin_is_never_surfaced_as_stale(tmp_path):
    conn = _conn(tmp_path)
    old_day = AS_OF - timedelta(days=20)
    action_id = "2" * 64
    _seed_pin(conn, 952, action_id, old_day)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    assert mark_pin_posted(conn, USER_ID, action_id) is True
    rebuild_ops(conn)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    stale = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)

    assert stale == []


def test_a_stale_unposted_caption_is_surfaced_by_its_own_channel(tmp_path):
    conn = _conn(tmp_path)
    old_day = AS_OF - timedelta(days=20)
    action_id = "3" * 64
    _seed_caption(conn, 953, action_id, old_day, channel="instagram")
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    stale = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)

    assert len(stale) == 1
    assert stale[0].channel == "instagram"
    assert stale[0].listing_id == 953
    assert stale[0].days_stale == 20


def test_a_posted_caption_is_never_surfaced_as_stale(tmp_path):
    conn = _conn(tmp_path)
    old_day = AS_OF - timedelta(days=20)
    action_id = "4" * 64
    _seed_caption(conn, 954, action_id, old_day)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    assert mark_caption_posted(conn, USER_ID, action_id) is True

    cfg = ops_config.get_ops_config(conn, USER_ID)
    stale = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)

    assert stale == []


def test_a_recent_draft_is_not_yet_stale(tmp_path):
    conn = _conn(tmp_path)
    recent_day = AS_OF - timedelta(days=3)  # inside the default 14d threshold
    action_id = "5" * 64
    _seed_pin(conn, 955, action_id, recent_day)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    stale = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)

    assert stale == []


def test_stale_at_exactly_the_threshold_boundary(tmp_path):
    """`cfg.social.staleness_days` days ago exactly -- still within the
    window (>, not >=, is what excludes a draft), so it IS surfaced."""
    conn = _conn(tmp_path)
    boundary_day = AS_OF - timedelta(days=14)  # exactly the default threshold
    action_id = "6" * 64
    _seed_pin(conn, 958, action_id, boundary_day)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    stale = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)

    assert len(stale) == 1
    assert stale[0].listing_id == 958


def test_deterministic_for_a_fixed_as_of(tmp_path):
    conn = _conn(tmp_path)
    old_day = AS_OF - timedelta(days=20)
    action_id = "7" * 64
    _seed_pin(conn, 956, action_id, old_day)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    first = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)
    second = analytics.stale_drafts(conn, USER_ID, cfg, as_of=AS_OF)

    assert first == second


def test_data_quality_notes_summarizes_stale_drafts_by_channel(tmp_path):
    conn = _conn(tmp_path)
    old_day = AS_OF - timedelta(days=20)
    action_id = "8" * 64
    _seed_pin(conn, 957, action_id, old_day)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    notes = analytics.data_quality_notes(conn, USER_ID, cfg, as_of=AS_OF)

    assert any("pin draft(s)" in n and "no posted event" in n for n in notes)
