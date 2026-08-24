"""`ops mark-posted` (Fix 1, 2026-08-24 follow-up): completes the
`social.pinterest_post` Variant A feedback loop -- appends `social.pin_posted`
for a drafted pin's own action_id, and brief.py's PINS TO POST queue excludes
any draft once its action_id is marked posted. Mirrors the `ops
approve`/`reject`/`undo` CLI test pattern in test_ops_cli_verbs.py."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief
from shopsteward.pipeline.ops.capabilities.pinterest_post import SocialPinterestPost, mark_posted
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run
from tests.pipeline.ops.helpers import seed_listing_observed_on

runner = CliRunner()

USER_ID = 1
TODAY = datetime.now(UTC).date()
LISTING_ID = 951


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _seed_image(conn, listing_id: int) -> None:
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.images.observed",
            payload={
                "listing_id": listing_id,
                "images": [{"listing_image_id": 1, "rank": 1, "url_570xN": "https://x/1.jpg"}],
            },
        ),
    )


def _draft_a_real_pin(conn, listing_id: int) -> str:
    """Runs the real `social.pinterest_post` materialize -> propose -> approve
    path (test_pin_experiments.py's real-draft precedent) so
    destination_url carries a genuine action_id[:12] prefix. Returns the
    full action_id."""
    seed_listing_observed_on(conn, listing_id=listing_id, title="Test Print", day=TODAY, views=10)
    _seed_image(conn, listing_id)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cap = SocialPinterestPost()
    register(cap)
    cfg = ops_config.load_ops_config()
    action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        ProposalIntent(
            capability_key="social.pinterest_post",
            target_id=str(listing_id),
            params={
                "title": "Test Pin",
                "description": "desc",
                "alt_text": "alt",
                "board_key": "wall_art",
            },
            reason="test",
        ),
    )
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    return action_id


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


# --- mark_posted() (underlying function) --------------------------------------


def test_undrafted_pin_still_appears_in_brief_pin_drafts(conn):
    action_id = _draft_a_real_pin(conn, LISTING_ID)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    brief = generate_brief(conn, USER_ID, cfg, as_of=TODAY)
    assert [p.action_id for p in brief.pin_drafts] == [action_id]


def test_mark_posted_drops_it_from_brief_pin_drafts(conn):
    action_id = _draft_a_real_pin(conn, LISTING_ID)
    appended = mark_posted(conn, USER_ID, action_id)
    assert appended is True

    posted = [e for e in read_all(conn, "social.pin_posted") if e.payload["action_id"] == action_id]
    assert len(posted) == 1
    assert posted[0].payload["listing_id"] == LISTING_ID

    cfg = ops_config.get_ops_config(conn, USER_ID)
    brief = generate_brief(conn, USER_ID, cfg, as_of=TODAY)
    assert brief.pin_drafts == []


def test_mark_posted_twice_is_a_safe_noop(conn):
    action_id = _draft_a_real_pin(conn, LISTING_ID)
    assert mark_posted(conn, USER_ID, action_id) is True
    assert mark_posted(conn, USER_ID, action_id) is False

    posted = [e for e in read_all(conn, "social.pin_posted") if e.payload["action_id"] == action_id]
    assert len(posted) == 1  # never double-appended


def test_mark_posted_unknown_action_id_raises_clearly(conn):
    # Well-formed (64-char) but matches no drafted pin.
    with pytest.raises(ValueError, match="no drafted pin found"):
        mark_posted(conn, USER_ID, "0" * 64)
    assert read_all(conn, "social.pin_posted") == []  # no partial state


def test_mark_posted_rejects_a_bare_utm_content_prefix(conn):
    # A 12-char utm_content prefix must not be accepted as if it were the
    # real action_id -- it would match action_id[:12] == action_id and get
    # stored as a truncated id _pin_drafts()'s full-id exclusion never
    # recognizes.
    action_id = _draft_a_real_pin(conn, LISTING_ID)
    with pytest.raises(ValueError, match="full 64-char id"):
        mark_posted(conn, USER_ID, action_id[:12])
    assert read_all(conn, "social.pin_posted") == []  # no partial state


def test_marking_one_pin_posted_does_not_hide_a_different_still_open_pin_for_the_same_listing(
    conn,
):
    """Cooldown-permitting a listing can have more than one pin over time --
    marking one action_id posted must not hide a sibling draft for the same
    listing_id (action_id-level matching, not listing_id-level)."""
    action_a = _draft_a_real_pin(conn, LISTING_ID)

    # A second, later drafted pin for the SAME listing (cooldown bypassed by
    # seeding directly -- real cooldown enforcement is pinterest_post.py's
    # own concern, not this test's).
    action_b_id = "b" * 40
    later = TODAY.isoformat() + "T00:00:01.000000Z"

    def _insert(event_type: str, payload: dict) -> None:
        conn.execute(
            "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (USER_ID, event_type, json.dumps(payload), later),
        )

    _insert(
        "action.proposed",
        {
            "action_id": action_b_id,
            "capability": "social.pinterest_post",
            "target_type": "listing",
            "target_id": str(LISTING_ID),
            "tier": 2,
            "reason": "x",
            "inputs_hash": "x",
            "estimated_cost_usd": 0.0,
            "undo_available": False,
            "expires_at": (TODAY + timedelta(days=14)).isoformat(),
            "params": {},
        },
    )
    _insert(
        "social.pin_drafted",
        {
            "listing_id": LISTING_ID,
            "title": "y",
            "description": "y",
            "alt_text": "y",
            "board_key": "wall_art",
            "destination_url": (
                f"https://www.etsy.com/listing/{LISTING_ID}?utm_content={action_b_id[:12]}"
            ),
            "image_url": "https://x/2.jpg",
            "drafted_at": later,
        },
    )
    _insert(
        "action.executed",
        {
            "action_id": action_b_id,
            "before": {},
            "after": {},
            "cost_usd": 0.0,
            "duration_ms": 0,
        },
    )
    conn.commit()

    mark_posted(conn, USER_ID, action_a)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    brief = generate_brief(conn, USER_ID, cfg, as_of=TODAY)
    assert [p.action_id for p in brief.pin_drafts] == [action_b_id]


# --- CLI ------------------------------------------------------------------


def _seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    conn = connect(db)
    migrate(conn)
    action_id = _draft_a_real_pin(conn, LISTING_ID)
    conn.close()
    return db, action_id


def test_cli_mark_posted_excludes_it_from_the_next_brief(tmp_path, monkeypatch):
    db, action_id = _seeded_db(tmp_path, monkeypatch)

    result = runner.invoke(app, ["ops", "mark-posted", action_id])

    assert result.exit_code == 0, result.output
    assert "marked posted" in result.output

    conn = connect(db)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    rebuild_ops(conn)
    brief = generate_brief(conn, USER_ID, cfg, as_of=TODAY)
    assert brief.pin_drafts == []


def test_cli_mark_posted_twice_is_a_safe_noop(tmp_path, monkeypatch):
    db, action_id = _seeded_db(tmp_path, monkeypatch)

    first = runner.invoke(app, ["ops", "mark-posted", action_id])
    second = runner.invoke(app, ["ops", "mark-posted", action_id])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "already marked posted" in second.output

    conn = connect(db)
    posted = [e for e in read_all(conn, "social.pin_posted") if e.payload["action_id"] == action_id]
    assert len(posted) == 1


def test_cli_mark_posted_unknown_action_id_exits_nonzero_without_a_traceback(tmp_path, monkeypatch):
    db, _ = _seeded_db(tmp_path, monkeypatch)

    result = runner.invoke(app, ["ops", "mark-posted", "0" * 64])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "no drafted pin found" in result.output
