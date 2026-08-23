from pathlib import Path

import pytest

from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.sync import read_live_observed, sync_etsy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "etsy"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_sync_appends_observation_events(conn):
    result = sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    assert result.shops == 1 and result.listings == 7 and result.receipts == 10
    types = [e.type for e in read_all(conn)]
    assert types.count("etsy.shop.observed") == 1
    assert types.count("etsy.listing.observed") == 7
    assert types.count("etsy.sale.observed") == 10


def test_resync_is_incremental_on_receipts(conn):
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    sales = read_all(conn, "etsy.sale")
    assert len(sales) == 10  # second sync must not duplicate any receipt


def test_cursor_is_per_user(conn):
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    result = sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=2)
    assert result.receipts == 10  # user 2's first sync must not inherit user 1's cursor


def test_same_timestamp_receipt_not_skipped_but_deduped(conn):
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    result = sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    # boundary receipt is refetched (min_created inclusive) but deduped by id
    assert result.receipts == 0
    assert len(read_all(conn, "etsy.sale")) == 10


def _tokens(shop_id: int) -> EtsyTokens:
    return EtsyTokens(
        access_token="t",
        access_expires_at=9999999999.0,
        refresh_token="r",
        shop_id=shop_id,
        etsy_user_id=1,
        scopes=["shops_r"],
    )


def test_read_live_observed_excludes_fixture_rows_that_predate_the_real_shop(conn, tmp_path):
    # fixture-era shop + listing (shop_id=100001, listing_id=111) -- these
    # predate any real shop.observed and must be excluded.
    append(conn, Event(user_id=1, type="etsy.shop.observed", payload={"shop_id": 100001}))
    append(
        conn,
        Event(user_id=1, type="etsy.listing.observed", payload={"listing_id": 111}),
    )
    # real shop + listing observed afterward, for the configured shop.
    append(conn, Event(user_id=1, type="etsy.shop.observed", payload={"shop_id": 52644245}))
    append(
        conn,
        Event(user_id=1, type="etsy.listing.observed", payload={"listing_id": 1820850226}),
    )

    store = EtsyTokenStore(path=tmp_path / "etsy_tokens.json")
    store.save(_tokens(52644245))

    events = read_live_observed(conn, "etsy.listing.observed", token_store=store)

    assert [e.payload["listing_id"] for e in events] == [1820850226]


def test_read_live_observed_anchors_on_first_shop_observed_not_latest(conn, tmp_path):
    # Real shop.observed, then a real sale, then a SECOND real shop.observed
    # for the same shop (a second `sync --live` call), then a second sale.
    # Both sales must be visible -- the anchor must not advance past the
    # first one and orphan the sale from the earlier real sync.
    append(conn, Event(user_id=1, type="etsy.shop.observed", payload={"shop_id": 52644245}))
    append(conn, Event(user_id=1, type="etsy.sale.observed", payload={"receipt_id": 1}))
    append(conn, Event(user_id=1, type="etsy.shop.observed", payload={"shop_id": 52644245}))
    append(conn, Event(user_id=1, type="etsy.sale.observed", payload={"receipt_id": 2}))

    store = EtsyTokenStore(path=tmp_path / "etsy_tokens.json")
    store.save(_tokens(52644245))

    events = read_live_observed(conn, "etsy.sale.observed", token_store=store)

    assert [e.payload["receipt_id"] for e in events] == [1, 2]


def test_read_live_observed_falls_back_unfiltered_with_no_matching_anchor(conn, tmp_path):
    # No etsy.shop.observed at all for the configured shop -- nothing to
    # anchor against, so everything is returned (fresh DB / directly-seeded
    # fixtures, the shape almost all of the existing test suite uses).
    append(
        conn,
        Event(user_id=1, type="etsy.listing.observed", payload={"listing_id": 111}),
    )

    store = EtsyTokenStore(path=tmp_path / "etsy_tokens.json")
    store.save(_tokens(52644245))

    events = read_live_observed(conn, "etsy.listing.observed", token_store=store)

    assert [e.payload["listing_id"] for e in events] == [111]


def test_read_live_observed_falls_back_unfiltered_with_no_token_store_on_disk(conn, tmp_path):
    # No Etsy auth on disk at all -- the common case for the rest of the
    # test suite, which never touches Etsy auth.
    append(
        conn,
        Event(user_id=1, type="etsy.listing.observed", payload={"listing_id": 111}),
    )

    store = EtsyTokenStore(path=tmp_path / "does-not-exist.json")

    events = read_live_observed(conn, "etsy.listing.observed", token_store=store)

    assert [e.payload["listing_id"] for e in events] == [111]
