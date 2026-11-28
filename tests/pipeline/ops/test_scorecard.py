"""THE NUMBERS: contribution-profit scorecard from events + projections only."""

import json
from datetime import date, timedelta

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed

USER_ID = 1
AS_OF = date(2026, 12, 10)


def _insert(conn, type_: str, payload: dict, day: date) -> None:
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (USER_ID, type_, json.dumps(payload), f"{day.isoformat()}T00:00:00.000000Z"),
    )
    conn.commit()


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    return conn


def _brief(conn):
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    return generate_brief(conn, USER_ID, cfg, as_of=AS_OF)


def test_contribution_math_for_a_digital_sale_and_listing_fee(tmp_path):
    conn = _conn(tmp_path)
    # 2 units @ $10 in ONE order 5 days ago; one listing fee published 3 days ago.
    seed_sale_observed(
        conn, receipt_id=1, day=AS_OF - timedelta(days=5), transactions=[(11, 101, 2, 10.0)]
    )
    _insert(conn, "gate3.published", {"draft_id": "d"}, AS_OF - timedelta(days=3))
    # An old sale: inside 365d but outside 30d.
    seed_sale_observed(
        conn, receipt_id=2, day=AS_OF - timedelta(days=100), transactions=[(11, 102, 1, 50.0)]
    )

    sc = _brief(conn).scorecard
    w30, w90, w365 = sc.windows
    assert (w30.days, w90.days, w365.days) == (30, 90, 365)
    assert w30.revenue_usd == 20.0 and w30.units == 2 and w30.orders == 1
    assert w30.transaction_fees_usd == 1.30  # 6.5%
    assert w30.processing_fees_usd == 0.85  # 3% + $0.25 per order
    assert w30.listing_fees_usd == 0.20
    assert w30.contribution_usd == round(20 - 1.30 - 0.85 - 0.20, 2)
    assert w90.revenue_usd == 20.0  # the $50 sale is 100d old
    assert w365.revenue_usd == 70.0 and w365.orders == 2
    text = render_text(_brief(conn))
    assert "THE NUMBERS" in text and "CONTRIBUTION" in text


def test_pod_sale_is_charged_the_matched_variant_base_cost(tmp_path):
    conn = _conn(tmp_path)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={"draft_id": "d", "etsy_listing_id": 22, "etsy_listing_state": "active"},
        ),
    )
    seed_sale_observed(
        conn, receipt_id=3, day=AS_OF - timedelta(days=2), transactions=[(22, 201, 1, 89.0)]
    )
    sc = _brief(conn).scorecard
    # linked but no priced variant known -> cost UNKNOWN, never silently 0
    assert sc.windows[0].fulfillment_usd == 0.0
    assert sc.windows[0].pod_units_cost_unknown == 1
    assert any("UNDER-stated" in n for n in sc.notes)


def test_traffic_is_too_sparse_without_daily_snapshots(tmp_path):
    conn = _conn(tmp_path)
    seed_listing_observed_on(
        conn, listing_id=11, title="A", day=AS_OF - timedelta(days=1), views=10
    )
    sc = _brief(conn).scorecard
    assert sc.traffic.listings_covered == 0
    assert sc.traffic.views_current is None
    assert any("too sparse" in n for n in sc.notes)
    assert "not measurable" in render_text(_brief(conn))


def test_traffic_compares_last_7d_with_prior_7d_from_dense_snapshots(tmp_path):
    conn = _conn(tmp_path)
    # views +10/day in the prior week, +5/day in the last week; favorites +1/day then +2/day.
    views, favs = 0, 0
    for i in range(14):
        day = AS_OF - timedelta(days=13 - i)
        seed_listing_observed_on(
            conn, listing_id=11, title="A", day=day, views=views, num_favorers=favs
        )
        views += 10 if i < 7 else 5
        favs += 1 if i < 7 else 2
    sc = _brief(conn).scorecard
    t = sc.traffic
    assert t.listings_covered == 1
    assert t.views_prior == 60.0  # 6 increments x 10 within the prior window
    assert t.views_current > 0 and t.views_current < t.views_prior
    text = render_text(_brief(conn))
    assert "market/seasonality context" in text
