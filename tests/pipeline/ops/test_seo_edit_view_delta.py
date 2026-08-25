"""T6 (2026-08-25): `analytics.seo_edit_view_delta()` -- the "make sure the
edit's timestamp is joinable" half of the per-listing-cooldown item.
`proj_listing_daily` already carries the "before" for free (module
docstring); this just proves the join from an executed `listing.seo_edit`
action_id to that history actually resolves, with the same before/after
views-per-day shape `analytics.pin_experiment_readout()` already uses."""

import json
from datetime import date, timedelta

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
LISTING_ID = 941
AS_OF = date(2026, 6, 1)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_seo_edit_executed(conn, action_id: str, listing_id: int, day: date) -> None:
    """Directly INSERTs the (action.proposed, action.executed) pair a real
    approve+execute run would produce, stamped at `day` -- lets the test
    place the edit arbitrarily far in the past (test_pin_experiments.py's
    `_seed_pin_action` precedent). Still append-only."""
    created_at = f"{day.isoformat()}T00:00:00.000000Z"
    proposed_payload = {
        "action_id": action_id,
        "capability": "listing.seo_edit",
        "target_type": "listing",
        "target_id": str(listing_id),
        "tier": 2,
        "reason": "test-seeded",
        "inputs_hash": "deadbeef",
        "estimated_cost_usd": 0.0,
        "undo_available": True,
        "expires_at": (day + timedelta(days=14)).isoformat(),
        "params": {},
    }
    executed_payload = {
        "action_id": action_id,
        "before": {"title": "Old Title"},
        "after": {"title": "New Title"},
        "cost_usd": 0.0,
        "duration_ms": 0,
    }
    for event_type, payload in (
        ("action.proposed", proposed_payload),
        ("action.executed", executed_payload),
    ):
        conn.execute(
            "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (USER_ID, event_type, json.dumps(payload), created_at),
        )
    conn.commit()


def test_view_delta_join_resolves_a_real_before_after_reading(tmp_path):
    conn = _conn(tmp_path)
    edited_at = AS_OF - timedelta(days=10)

    seed_listing_observed_on(
        conn, listing_id=LISTING_ID, title="New Title", day=edited_at - timedelta(days=7), views=100
    )
    seed_listing_observed_on(
        conn, listing_id=LISTING_ID, title="New Title", day=edited_at - timedelta(days=1), views=114
    )  # 2.0/day
    seed_listing_observed_on(
        conn, listing_id=LISTING_ID, title="New Title", day=edited_at + timedelta(days=1), views=114
    )
    seed_listing_observed_on(
        conn, listing_id=LISTING_ID, title="New Title", day=edited_at + timedelta(days=7), views=163
    )  # 7.0/day

    _seed_seo_edit_executed(conn, "seo-view-delta-1", LISTING_ID, edited_at)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.load_ops_config()
    result = analytics.seo_edit_view_delta(conn, USER_ID, "seo-view-delta-1", cfg, as_of=AS_OF)

    assert result is not None
    assert result.listing_id == LISTING_ID
    assert result.edited_at == edited_at.isoformat()
    assert result.baseline_views_per_day == 2.0
    assert result.observed_views_per_day == 7.0
    assert result.delta_views_per_day == 5.0


def test_view_delta_returns_none_for_an_unknown_action_id(tmp_path):
    conn = _conn(tmp_path)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cfg = ops_config.load_ops_config()
    assert analytics.seo_edit_view_delta(conn, USER_ID, "never-executed", cfg, as_of=AS_OF) is None
