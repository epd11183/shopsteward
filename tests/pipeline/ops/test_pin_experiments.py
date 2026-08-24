"""P1 outcome projection + experiment readout (2026-08-24 design doc §3) --
`proj_pin_experiments` folds `social.pin_drafted` events (paired with the
action_id of the action.executed that produced them) and
`analytics.pin_experiment_readout()` reads a correlational, NOT
attribution, before/after views-per-day delta over `proj_listing_daily`.
Read-only: no touch to pinterest_post.py's eligibility/execute logic."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.capabilities.pinterest_post import SocialPinterestPost
from shopsteward.pipeline.ops.models import Brief
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()


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


def _cfg():
    return ops_config.load_ops_config()


def _fmt(dt: datetime) -> str:
    # core/db.py's schema-default format -- pinterest_post.py's cooldown
    # cutoff comparisons and the pin-experiment fold both compare lexically
    # against this exact format (test_pinterest_post.py precedent).
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed_pin_action(conn, *, listing_id: int, action_id: str, drafted_at: datetime) -> None:
    """Directly INSERTs the three events one real approve+execute run would
    produce (action.proposed / social.pin_drafted / action.executed), all
    stamped at `drafted_at` -- lets tests place a pin arbitrarily far in the
    past, which the real capability (always "now") cannot do. Still
    append-only: a plain INSERT, never an UPDATE/DELETE (helpers.py
    precedent)."""
    created_at = _fmt(drafted_at)
    rows = [
        (
            "action.proposed",
            {
                "action_id": action_id,
                "capability": "social.pinterest_post",
                "target_type": "listing",
                "target_id": str(listing_id),
                "tier": 2,
                "reason": "test-seeded",
                "inputs_hash": "deadbeef",
                "estimated_cost_usd": 0.0,
                "undo_available": False,
                "expires_at": (drafted_at.date() + timedelta(days=14)).isoformat(),
                "params": {},
            },
        ),
        (
            "social.pin_drafted",
            {
                "listing_id": listing_id,
                "title": "x",
                "description": "x",
                "alt_text": "x",
                "board_key": "wall_art",
                "destination_url": (
                    f"https://www.etsy.com/listing/{listing_id}"
                    f"?utm_source=pinterest&utm_medium=social&utm_campaign=shopsteward"
                    f"&utm_content={action_id[:12]}"
                ),
                "image_url": "https://example.com/img.jpg",
                "drafted_at": drafted_at.isoformat(),
            },
        ),
        (
            "action.executed",
            {
                "action_id": action_id,
                "before": {},
                "after": {"listing_id": listing_id, "board_key": "wall_art"},
                "cost_usd": 0.0,
                "duration_ms": 0,
            },
        ),
    ]
    for event_type, payload in rows:
        conn.execute(
            "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (USER_ID, event_type, json.dumps(payload), created_at),
        )
    conn.commit()


def _seed_daily(conn, listing_id: int, day: date, views: int) -> None:
    seed_listing_observed_on(
        conn, listing_id=listing_id, title=f"L{listing_id}", day=day, views=views
    )


# --- proj_pin_experiments fold (real end-to-end draft) -----------------------


def _seed_image(conn, listing_id):
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


def test_projection_pairs_a_real_drafted_pin_with_its_own_action_id(conn):
    listing_id = 801
    seed_listing_observed_on(
        conn, listing_id=listing_id, title="Real Pin Print", day=TODAY, views=10
    )
    _seed_image(conn, listing_id)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cap = SocialPinterestPost()
    register(cap)
    cfg = _cfg()
    from shopsteward.adapters.planner.interface import ProposalIntent

    action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        ProposalIntent(
            capability_key="social.pinterest_post",
            target_id=str(listing_id),
            params={
                "title": "Real Pin",
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

    row = conn.execute(
        "SELECT * FROM proj_pin_experiments WHERE user_id=? AND listing_id=?",
        (USER_ID, listing_id),
    ).fetchone()
    assert row is not None
    assert row["action_id"] == action_id
    assert row["drafted_at"][:10] == TODAY.isoformat()


def _insert_event(conn, event_type: str, payload: dict, created_at: str) -> None:
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (USER_ID, event_type, json.dumps(payload), created_at),
    )
    conn.commit()


def _destination_url(listing_id: int, action_id: str) -> str:
    return (
        f"https://www.etsy.com/listing/{listing_id}"
        f"?utm_source=pinterest&utm_medium=social&utm_campaign=shopsteward"
        f"&utm_content={action_id[:12]}"
    )


def _proposed_payload(listing_id: int, action_id: str) -> dict:
    return {
        "action_id": action_id,
        "capability": "social.pinterest_post",
        "target_type": "listing",
        "target_id": str(listing_id),
        "tier": 2,
        "reason": "test-seeded",
        "inputs_hash": "deadbeef",
        "estimated_cost_usd": 0.0,
        "undo_available": False,
        "expires_at": (TODAY + timedelta(days=14)).isoformat(),
        "params": {},
    }


def _drafted_payload(listing_id: int, action_id: str, drafted_at: datetime) -> dict:
    return {
        "listing_id": listing_id,
        "title": "x",
        "description": "x",
        "alt_text": "x",
        "board_key": "wall_art",
        "destination_url": _destination_url(listing_id, action_id),
        "image_url": "https://example.com/img.jpg",
        "drafted_at": drafted_at.isoformat(),
    }


def _executed_payload(listing_id: int, action_id: str) -> dict:
    return {
        "action_id": action_id,
        "before": {},
        "after": {"listing_id": listing_id, "board_key": "wall_art"},
        "cost_usd": 0.0,
        "duration_ms": 0,
    }


def test_pin_experiments_not_swapped_when_execution_order_reverses_proposal_order(conn):
    """Review finding: action_rows() is proposal-ordered, social.pin_drafted
    events are execution-ordered. Propose A (day 1) then B (day 2), but
    execute/draft B before A (fully reachable -- pinterest_post's max_tier
    is Tier.PROPOSE, so the operator controls approval order independently
    of proposal order). The old zip(drafts, action_rows()) logic paired
    them positionally and swapped the action_ids; the utm_content-based fix
    must attribute each drafted event to its OWN action_id regardless of
    order."""
    listing_id = 901
    action_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    action_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    day1 = _fmt(TODAY - timedelta(days=2))
    day2 = _fmt(TODAY - timedelta(days=1))
    drafted_b_at = TODAY - timedelta(days=1)
    drafted_a_at = TODAY

    # Proposal order: A, then B.
    _insert_event(conn, "action.proposed", _proposed_payload(listing_id, action_a), day1)
    _insert_event(conn, "action.proposed", _proposed_payload(listing_id, action_b), day2)

    # Execution/draft order: B, then A -- the reverse.
    _insert_event(
        conn, "social.pin_drafted", _drafted_payload(listing_id, action_b, drafted_b_at), day2
    )
    _insert_event(conn, "action.executed", _executed_payload(listing_id, action_b), day2)
    _insert_event(
        conn, "social.pin_drafted", _drafted_payload(listing_id, action_a, drafted_a_at), day1
    )
    _insert_event(conn, "action.executed", _executed_payload(listing_id, action_a), day1)

    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)

    rows = {
        r["action_id"]: r
        for r in conn.execute(
            "SELECT * FROM proj_pin_experiments WHERE user_id=? AND listing_id=?",
            (USER_ID, listing_id),
        ).fetchall()
    }
    assert set(rows) == {action_a, action_b}
    assert rows[action_a]["drafted_at"] == drafted_a_at.isoformat()
    assert rows[action_b]["drafted_at"] == drafted_b_at.isoformat()


# --- analytics.pin_experiment_readout() ---------------------------------------


def test_readout_measurable_pin_shows_real_delta(conn):
    listing_id = 811
    drafted_at = datetime.combine(TODAY - timedelta(days=10), datetime.min.time(), tzinfo=UTC)
    drafted_date = drafted_at.date()

    before_start = drafted_date - timedelta(days=7)
    before_end = drafted_date - timedelta(days=1)
    after_start = drafted_date + timedelta(days=1)
    after_end = drafted_date + timedelta(days=7)

    _seed_daily(conn, listing_id, before_start, views=100)
    _seed_daily(conn, listing_id, before_end, views=114)  # +14 over 7d -> 2.0/day
    _seed_daily(conn, listing_id, after_start, views=114)
    _seed_daily(conn, listing_id, after_end, views=163)  # +49 over 7d -> 7.0/day

    _seed_pin_action(conn, listing_id=listing_id, action_id="act-measurable", drafted_at=drafted_at)

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = _cfg()
    results = analytics.pin_experiment_readout(conn, USER_ID, cfg, as_of=TODAY)
    assert len(results) == 1
    r = results[0]
    assert r.action_id == "act-measurable"
    assert r.baseline_views_per_day == pytest.approx(2.0)
    assert r.observed_views_per_day == pytest.approx(7.0)
    assert r.delta_views_per_day == pytest.approx(5.0)


def test_readout_too_recent_pin_omits_observed_not_zero(conn):
    listing_id = 812
    drafted_at = datetime.combine(TODAY - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    _seed_pin_action(conn, listing_id=listing_id, action_id="act-too-recent", drafted_at=drafted_at)

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = _cfg()
    results = analytics.pin_experiment_readout(conn, USER_ID, cfg, as_of=TODAY)
    assert len(results) == 1
    assert results[0].observed_views_per_day is None
    assert results[0].delta_views_per_day is None


def test_readout_insufficient_prior_history_omits_baseline_not_zero(conn):
    listing_id = 813
    drafted_at = datetime.combine(TODAY - timedelta(days=10), datetime.min.time(), tzinfo=UTC)
    drafted_date = drafted_at.date()

    # Listing's earliest observation is only 3 days before the pin -- less
    # than the 7-day baseline window needs.
    _seed_daily(conn, listing_id, drafted_date - timedelta(days=3), views=50)
    after_start = drafted_date + timedelta(days=1)
    after_end = drafted_date + timedelta(days=7)
    _seed_daily(conn, listing_id, after_start, views=60)
    _seed_daily(conn, listing_id, after_end, views=95)

    _seed_pin_action(
        conn, listing_id=listing_id, action_id="act-no-baseline", drafted_at=drafted_at
    )

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = _cfg()
    results = analytics.pin_experiment_readout(conn, USER_ID, cfg, as_of=TODAY)
    assert len(results) == 1
    r = results[0]
    assert r.baseline_views_per_day is None
    assert r.observed_views_per_day == pytest.approx(5.0)
    assert r.delta_views_per_day is None


# --- Brief: PIN EXPERIMENTS section -------------------------------------------


def test_brief_shows_real_delta_for_a_measurable_pin(conn):
    listing_id = 821
    drafted_at = datetime.combine(TODAY - timedelta(days=10), datetime.min.time(), tzinfo=UTC)
    drafted_date = drafted_at.date()
    before_start = drafted_date - timedelta(days=7)
    before_end = drafted_date - timedelta(days=1)
    after_start = drafted_date + timedelta(days=1)
    after_end = drafted_date + timedelta(days=7)
    _seed_daily(conn, listing_id, before_start, views=10)
    _seed_daily(conn, listing_id, before_end, views=17)  # 1.0/day
    _seed_daily(conn, listing_id, after_start, views=17)
    _seed_daily(conn, listing_id, after_end, views=52)  # 5.0/day
    _seed_pin_action(conn, listing_id=listing_id, action_id="act-brief-real", drafted_at=drafted_at)

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    brief = generate_brief(conn, USER_ID, cfg, as_of=TODAY)

    assert len(brief.pin_experiments) == 1
    text = render_text(brief)
    assert "PIN EXPERIMENTS" in text
    assert "1.0 -> 5.0 views/day" in text
    assert "may be pin-driven, may be coincidental" in text


def test_brief_marks_a_too_recent_pin_as_too_early_not_zero(conn):
    listing_id = 822
    drafted_at = datetime.combine(TODAY - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    _seed_pin_action(
        conn, listing_id=listing_id, action_id="act-brief-early", drafted_at=drafted_at
    )

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    brief = generate_brief(conn, USER_ID, cfg, as_of=TODAY)

    assert len(brief.pin_experiments) == 1
    text = render_text(brief)
    assert "too early to measure" in text
    assert "0.0 views/day" not in text
    assert " 0 views/day" not in text


def test_brief_pin_experiments_section_disabled_by_config(conn):
    listing_id = 823
    drafted_at = datetime.combine(TODAY - timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    _seed_pin_action(conn, listing_id=listing_id, action_id="act-brief-off", drafted_at=drafted_at)

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    cfg = cfg.model_copy(
        update={"brief_sections": cfg.brief_sections.model_copy(update={"pin_experiments": False})}
    )
    brief = generate_brief(conn, USER_ID, cfg, as_of=TODAY)

    assert brief.pin_experiments == []
    text = render_text(brief)
    assert "PIN EXPERIMENTS" not in text


def test_existing_brief_construction_stays_green_new_field_defaults_empty():
    brief = Brief(generated_at=TODAY, window_days=7)
    assert brief.pin_experiments == []
