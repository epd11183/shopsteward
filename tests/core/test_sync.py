from pathlib import Path

import pytest

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.sync import sync_etsy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "etsy"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_sync_appends_observation_events(conn):
    result = sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    assert result.shops == 1 and result.listings == 3 and result.receipts == 2
    types = [e.type for e in read_all(conn)]
    assert types.count("etsy.shop.observed") == 1
    assert types.count("etsy.listing.observed") == 3
    assert types.count("etsy.sale.observed") == 2


def test_resync_is_incremental_on_receipts(conn):
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    sales = read_all(conn, "etsy.sale")
    assert len(sales) == 2  # second sync passes min_created past both receipts
