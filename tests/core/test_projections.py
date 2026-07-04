from pathlib import Path

import pytest

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import analytics_summary, rebuild
from shopsteward.core.sync import sync_etsy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "etsy"


@pytest.fixture()
def synced(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    rebuild(conn)
    return conn


def test_summary_totals(synced):
    s = analytics_summary(synced, user_id=1)
    assert s.total_revenue_usd == pytest.approx(72.0)  # 25.00 + 47.00
    assert s.total_orders == 2
    assert s.active_listings == 3


def test_top_listings_by_views(synced):
    s = analytics_summary(synced, user_id=1)
    assert s.top_listings[0].listing_id == 222  # 340 views


def test_rebuild_is_idempotent(synced):
    rebuild(synced)
    rebuild(synced)
    assert analytics_summary(synced, user_id=1).total_orders == 2
