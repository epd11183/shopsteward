"""Per-action OUTCOMES readout, baseline-adjusted against control listings."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from shopsteward.adapters.planner.interface import PlannerLimits
from shopsteward.adapters.planner.openrouter import _plan_system_prompt
from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops import outcomes
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.planner import _build_facts_json
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed

USER_ID = 1
AS_OF = date(2026, 12, 20)
ANCHOR = AS_OF - timedelta(days=10)
TARGET = 1


def _insert(conn, type_: str, payload: dict, day: date) -> None:
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (USER_ID, type_, json.dumps(payload), f"{day.isoformat()}T12:00:00.000000Z"),
    )
    conn.commit()


def _execute(conn, capability: str, listing_id: int, day: date, tag: str = "a") -> str:
    action_id = f"{tag}-{capability}-{listing_id}"
    _insert(
        conn,
        "action.proposed",
        {
            "action_id": action_id,
            "capability": capability,
            "target_type": "listing",
            "target_id": str(listing_id),
            "tier": 0,
            "reason": "r",
            "inputs_hash": "h",
            "estimated_cost_usd": 0.0,
            "undo_available": True,
            "expires_at": (day + timedelta(days=14)).isoformat(),
            "params": {},
        },
        day,
    )
    _insert(conn, "action.approved", {"action_id": action_id, "by": "tier:T0"}, day)
    _insert(
        conn,
        "action.executed",
        {"action_id": action_id, "before": {}, "after": {}, "cost_usd": 0.0, "duration_ms": 1},
        day,
    )
    return action_id


def _snapshots(conn, listing_id: int, *, before_rate: int, after_rate: int) -> None:
    views = 0
    for offset in range(-20, 11):  # 20d before the anchor .. 10d after (== AS_OF)
        day = ANCHOR + timedelta(days=offset)
        seed_listing_observed_on(
            conn, listing_id=listing_id, title=f"L{listing_id}", day=day, views=views
        )
        views += before_rate if day <= ANCHOR else after_rate


def _build(tmp_path, n_listings: int, *, before_rate=10, after_rate=5):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    for lid in range(1, n_listings + 1):
        _snapshots(conn, lid, before_rate=before_rate, after_rate=after_rate)
    return conn


def _finish(conn):
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    return cfg, generate_brief(conn, USER_ID, cfg, as_of=AS_OF)


def test_market_wide_50pct_drop_is_not_blamed_on_the_action(tmp_path):
    conn = _build(tmp_path, 8)  # every listing's views/day halves after the anchor
    _execute(conn, "listing.seo_edit", TARGET, ANCHOR)

    _, brief = _finish(conn)

    (best,) = brief.outcome_best
    w = best.windows[0]
    assert w.days == 7 and w.status == "measured"
    assert w.views_change_raw < 0  # the raw reading blames the action
    assert w.controls_n == 7
    assert w.views_change_adjusted == pytest.approx(0.0, abs=1e-9)
    (r,) = [r for r in brief.outcome_rollups if r.days == 7]
    assert r.capability == "listing.seo_edit"
    assert r.median_adjusted_views_per_day == pytest.approx(0.0, abs=1e-9)
    assert r.median_raw_views_per_day < 0
    text = render_text(brief)
    assert "OUTCOMES" in text and "vs shop trend" in text


def test_a_listing_that_underperforms_the_market_shows_a_negative_adjusted_change(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    for lid in range(2, 9):  # the market halves ...
        _snapshots(conn, lid, before_rate=10, after_rate=5)
    _snapshots(conn, TARGET, before_rate=10, after_rate=0)  # ... the target collapses
    _execute(conn, "listing.reprice", TARGET, ANCHOR)

    _, brief = _finish(conn)

    (only,) = brief.outcome_best
    w = only.windows[0]
    assert w.views_change_raw < w.control_median_change < 0
    assert w.views_change_adjusted < 0
    assert {
        d["listing_id"]
        for d in outcomes.planner_facts([only], ops_config.get_ops_config(conn, USER_ID))[
            "negative_on_listing"
        ]
    } == {TARGET}


def test_fewer_than_five_controls_is_no_baseline(tmp_path):
    conn = _build(tmp_path, 5)  # target + 4 controls
    _execute(conn, "listing.seo_edit", TARGET, ANCHOR)

    _, brief = _finish(conn)

    (r,) = [r for r in brief.outcome_rollups if r.days == 7]
    assert r.n_measured == 0 and r.n_no_baseline == 1
    assert brief.outcome_best == []
    assert "no baseline" in render_text(brief)


def test_controls_exclude_listings_with_their_own_action_in_the_span(tmp_path):
    conn = _build(tmp_path, 8)
    _execute(conn, "listing.seo_edit", TARGET, ANCHOR)
    for lid in (2, 3, 4):  # three more acted-on listings: only 4 controls left
        _execute(conn, "listing.renew", lid, ANCHOR + timedelta(days=1))

    _, brief = _finish(conn)

    seo = [r for r in brief.outcome_rollups if r.capability == "listing.seo_edit" and r.days == 7]
    assert seo[0].n_measured == 0 and seo[0].n_no_baseline == 1


def test_window_not_elapsed_and_uncovered_snapshots_are_labelled(tmp_path):
    conn = _build(tmp_path, 8)
    _execute(conn, "listing.seo_edit", TARGET, AS_OF - timedelta(days=2), tag="recent")
    # listing 99 has a single snapshot: its after-window is elapsed but uncovered.
    seed_listing_observed_on(conn, listing_id=99, title="L99", day=ANCHOR, views=5)
    _execute(conn, "listing.seo_edit", 99, ANCHOR, tag="thin")

    cfg, _ = _finish(conn)
    from shopsteward.pipeline.ops import outcomes

    by_id = {o.action_id: o for o in outcomes.action_outcomes(conn, USER_ID, cfg, AS_OF)}
    assert by_id["recent-listing.seo_edit-1"].windows[0].status == "not yet measurable"
    assert by_id["thin-listing.seo_edit-99"].windows[0].status == "not measurable"
    assert by_id["recent-listing.seo_edit-1"].windows[1].status == "not yet measurable"  # 28d


def test_sales_before_and_after_are_counted(tmp_path):
    conn = _build(tmp_path, 8)
    _execute(conn, "listing.seo_edit", TARGET, ANCHOR)
    seed_sale_observed(
        conn, receipt_id=1, day=ANCHOR - timedelta(days=3), transactions=[(TARGET, 11, 1, 10.0)]
    )
    seed_sale_observed(
        conn, receipt_id=2, day=ANCHOR + timedelta(days=2), transactions=[(TARGET, 12, 2, 10.0)]
    )

    _, brief = _finish(conn)

    w = brief.outcome_best[0].windows[0] if brief.outcome_best else None
    assert w is not None and (w.sales_before, w.sales_after) == (1, 2)


def test_planner_facts_carry_the_adjusted_rollup_and_the_prompt_is_hedged(tmp_path):
    conn = _build(tmp_path, 8)
    _execute(conn, "listing.seo_edit", TARGET, ANCHOR)
    cfg, _ = _finish(conn)

    facts = json.loads(
        _build_facts_json(conn, USER_ID, cfg, [], as_of=datetime(2026, 12, 20, tzinfo=UTC))
    )

    block = facts["action_outcomes"]
    (row,) = block["by_capability"]
    assert row["capability"] == "listing.seo_edit"
    assert row["n_actions"] == 1 and row["n_measured"] == 1
    assert row["median_adjusted_views_per_day_change"] == pytest.approx(0.0, abs=1e-9)
    assert block["negative_on_listing"] == []

    prompt = _plan_system_prompt(
        PlannerLimits(
            reprice_min_price_usd=3.0,
            reprice_max_pct_change=0.4,
            seo_edit_min_lifetime_views=5,
            caption_max_len=2200,
            pinterest_max_title_len=100,
            pinterest_max_description_len=500,
            pinterest_max_alt_text_len=500,
            pinterest_board_keys=["wall-art"],
        )
    )
    assert "relative to the shop-wide trend over the same window; samples are small." in prompt
    assert "avoid repeating ones with negative outcomes on the same listing" in prompt
