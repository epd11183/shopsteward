"""etsy.keyword.probed -- end-to-end probe against fixture data, tag
frequency, epoch-seconds conversion, own-listing exclusion, absence-is-not-
zero, empty result set, and determinism for a fixed as_of."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.adapters.etsy.models import EtsyActiveListingResult, Money
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops.config import load_ops_config
from shopsteward.pipeline.ops.keyword_probe import _compute_aggregates, probe_keyword
from tests.pipeline.ops.helpers import seed_listing_observed_on

FIXTURES = Path(__file__).parents[2] / "fixtures" / "etsy"
USER_ID = 1
AS_OF = datetime(2026, 8, 25, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    # 111 is OUR OWN listing (also present in active_listings.json's
    # "sandhill crane print" fixture) -- proves the exclusion.
    seed_listing_observed_on(
        c, listing_id=111, title="Our Own Sandhill Crane Print", day=AS_OF.date(), views=10
    )
    rebuild_core(c)
    return c


@pytest.fixture()
def cfg():
    return load_ops_config()


def test_probe_excludes_own_listing_and_computes_aggregates(conn, cfg):
    adapter = FixtureEtsyAdapter(FIXTURES)
    result = probe_keyword(conn, USER_ID, adapter, cfg, "sandhill crane print", as_of=AS_OF)

    assert result.competition_count == 5  # Etsy's total match count, unaffected by exclusion
    agg = result.aggregates
    assert agg.sample_size == 3  # 4 fixture rows minus our own listing (111)
    # our own listing's tags ("wall art") never inflate the competitor tag set
    assert agg.tag_frequency["bird print"] == 3
    assert agg.tag_frequency["sandhill crane"] == 2


def test_probe_stores_derived_aggregates_not_raw_rows(conn, cfg):
    adapter = FixtureEtsyAdapter(FIXTURES)
    probe_keyword(conn, USER_ID, adapter, cfg, "sandhill crane print", as_of=AS_OF)

    events = read_all(conn, "etsy.keyword.probed")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["phrase"] == "sandhill crane print"
    assert payload["competition_count"] == 5
    assert "aggregates" in payload
    # no raw listing rows (title, listing_id, url, exact per-row price/favorites)
    # anywhere in the stored payload -- only bounded, derived aggregates.
    assert "results" not in payload
    assert "title" not in payload["aggregates"]
    assert "url" not in payload["aggregates"]
    assert "900001" not in str(payload)  # no competitor listing_id leaked into payload


def test_probe_empty_result_set_no_divide_by_zero(conn, cfg):
    adapter = FixtureEtsyAdapter(FIXTURES)
    result = probe_keyword(conn, USER_ID, adapter, cfg, "no matches phrase", as_of=AS_OF)

    assert result.competition_count == 0
    agg = result.aggregates
    assert agg.sample_size == 0
    assert agg.tag_frequency == {}
    # absence, never a computed 0
    assert agg.median_price_usd is None
    assert agg.min_price_usd is None
    assert agg.max_price_usd is None
    assert agg.median_favorites_per_day is None


def test_probe_unknown_phrase_absence_is_not_zero(conn, cfg):
    adapter = FixtureEtsyAdapter(FIXTURES)
    result = probe_keyword(
        conn, USER_ID, adapter, cfg, "a phrase with zero fixture coverage", as_of=AS_OF
    )
    assert result.competition_count == 0
    assert result.aggregates.sample_size == 0
    assert result.aggregates.median_favorites_per_day is None


def test_probe_is_deterministic_for_a_fixed_as_of(conn, cfg):
    adapter = FixtureEtsyAdapter(FIXTURES)
    first = probe_keyword(conn, USER_ID, adapter, cfg, "sandhill crane print", as_of=AS_OF)
    second = probe_keyword(conn, USER_ID, adapter, cfg, "sandhill crane print", as_of=AS_OF)
    assert first.aggregates == second.aggregates


def _row(listing_id: int, *, created_ts: int, favorers: int) -> EtsyActiveListingResult:
    return EtsyActiveListingResult(
        listing_id=listing_id,
        title="T",
        tags=["t"],
        price=Money(amount=1000, divisor=100, currency_code="USD"),
        num_favorers=favorers,
        creation_timestamp=created_ts,
    )


def test_epoch_seconds_creation_timestamp_converts_to_favorites_per_day() -> None:
    # created exactly 10 days before as_of, epoch SECONDS (Etsy's real
    # shape) -- 20 favorites / 10 days = 2.0/day. Wrong-unit handling (e.g.
    # treating it as milliseconds) would produce a wildly different age.
    as_of = datetime(2026, 8, 25, tzinfo=UTC)
    ten_days_before = datetime(2026, 8, 15, tzinfo=UTC)
    row = _row(1, created_ts=int(ten_days_before.timestamp()), favorers=20)

    agg = _compute_aggregates([row], as_of)

    assert agg.median_favorites_per_day == pytest.approx(2.0)


def test_same_day_creation_does_not_divide_by_zero() -> None:
    as_of = datetime(2026, 8, 25, tzinfo=UTC)
    row = _row(1, created_ts=int(as_of.timestamp()), favorers=5)

    agg = _compute_aggregates([row], as_of)

    assert agg.median_favorites_per_day == pytest.approx(5.0)  # age floored to 1 day, not 0
