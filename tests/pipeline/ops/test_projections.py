from datetime import date, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import USER_ID, seed_listing_observed_on

LISTING_ID = 111


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_n_observations_across_n_distinct_days_produce_n_rows(conn):
    """The whole point of proj_listing_daily (design §0(a)/§9 slice 1):
    core/projections.py's proj_listings would INSERT OR REPLACE these five
    observations down to ONE row (last-write-wins by listing_id alone).
    Bucketing on the event's own day must instead keep all five."""
    start = date(2024, 1, 1)
    for i in range(5):
        seed_listing_observed_on(
            conn,
            listing_id=LISTING_ID,
            title="Test Listing",
            day=start + timedelta(days=i),
            views=100 + i,
        )
    rebuild_ops(conn)

    rows = conn.execute(
        "SELECT day, views FROM proj_listing_daily WHERE user_id=? AND listing_id=? ORDER BY day",
        (USER_ID, LISTING_ID),
    ).fetchall()
    assert len(rows) == 5
    assert [r["views"] for r in rows] == [100, 101, 102, 103, 104]
    assert [r["day"] for r in rows] == [(start + timedelta(days=i)).isoformat() for i in range(5)]


def test_same_day_resync_collapses_to_last_observation(conn):
    day = date(2024, 6, 1)
    seed_listing_observed_on(conn, listing_id=LISTING_ID, title="Test Listing", day=day, views=10)
    seed_listing_observed_on(conn, listing_id=LISTING_ID, title="Test Listing", day=day, views=20)

    rebuild_ops(conn)

    rows = conn.execute(
        "SELECT views FROM proj_listing_daily WHERE user_id=? AND listing_id=?",
        (USER_ID, LISTING_ID),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["views"] == 20  # last observation within the day wins


def test_rebuild_is_idempotent(conn):
    seed_listing_observed_on(
        conn, listing_id=LISTING_ID, title="Test Listing", day=date(2024, 1, 1), views=10
    )
    rebuild_ops(conn)
    rebuild_ops(conn)
    rows = conn.execute(
        "SELECT * FROM proj_listing_daily WHERE user_id=? AND listing_id=?", (USER_ID, LISTING_ID)
    ).fetchall()
    assert len(rows) == 1


def test_user_id_isolates_listings(conn):
    seed_listing_observed_on(
        conn, listing_id=LISTING_ID, title="Mine", day=date(2024, 1, 1), views=10, user_id=1
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_ID,
        title="Someone else's",
        day=date(2024, 1, 1),
        views=999,
        user_id=2,
    )
    rebuild_ops(conn)
    row = conn.execute(
        "SELECT views FROM proj_listing_daily WHERE user_id=1 AND listing_id=?", (LISTING_ID,)
    ).fetchone()
    assert row["views"] == 10


def test_proj_sale_items_folds_one_row_per_transaction(conn):
    from tests.pipeline.ops.helpers import seed_sale_observed

    seed_sale_observed(
        conn,
        receipt_id=1,
        day=date(2024, 1, 1),
        transactions=[(111, 1001, 2, 43.50), (222, 1002, 1, 19.00)],
    )
    rebuild_ops(conn)

    rows = conn.execute(
        "SELECT listing_id, quantity, price_usd FROM proj_sale_items WHERE user_id=? "
        "ORDER BY transaction_id",
        (USER_ID,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["listing_id"] == 111 and rows[0]["quantity"] == 2
    assert rows[1]["listing_id"] == 222 and rows[1]["quantity"] == 1
