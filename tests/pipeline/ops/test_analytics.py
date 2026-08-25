"""The one scenario the design's §11 asks for: a synthetic ~2-year event log
with a seller, a viewed-but-never-sold listing, a dead listing, and a
seasonal listing that a naive threshold must NOT mistake for dead. Every
assertion here is pure analytics.py output -- no LLM, no network (see
test_no_network_no_llm.py for the structural check)."""

from datetime import timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.config import load_ops_config
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import (
    AS_OF,
    LISTING_DEAD,
    LISTING_SEASONAL,
    LISTING_SELLER,
    LISTING_VIEWED_NOT_SOLD,
    USER_ID,
    seed_listing_observed_on,
    seed_sale_observed,
    seed_two_year_shop,
)


@pytest.fixture()
def shop(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    seed_two_year_shop(conn)
    rebuild_core(conn)
    rebuild_ops(conn)
    return conn


@pytest.fixture()
def cfg():
    return load_ops_config()


def test_revenue_window_vs_prior(shop, cfg):
    r = analytics.revenue_window(shop, USER_ID, cfg, as_of=AS_OF)
    assert r.current_usd == pytest.approx(174.0)
    assert r.prior_usd == pytest.approx(87.0)
    assert r.pct_change == pytest.approx(1.0)
    assert r.orders == 2
    assert r.units == 2


def test_revenue_window_pct_change_is_none_when_prior_is_zero(shop, cfg):
    # isolate: a fresh shop with sales only in the current window has no
    # denominator for a growth rate -- must not divide by zero or lie with 0%.
    from tests.pipeline.ops.helpers import seed_sale_observed

    conn = connect(":memory:")
    migrate(conn)
    seed_sale_observed(conn, receipt_id=1, day=AS_OF, transactions=[(1, 1, 1, 10.0)])
    rebuild_core(conn)
    rebuild_ops(conn)
    r = analytics.revenue_window(conn, USER_ID, cfg, as_of=AS_OF)
    assert r.prior_usd == 0.0
    assert r.pct_change is None


def test_revenue_window_includes_shipping_tax_but_item_totals_do_not(cfg):
    # F2: a real Etsy receipt's grandtotal is item price + shipping/tax.
    # revenue_window sums grandtotal (proj_sales); top_sellers/
    # product_type_breakdown sum item price only (proj_sale_items). Both are
    # correct numbers for what they claim -- the fixture must actually
    # produce a receipt where they disagree, or this proves nothing.
    from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed

    conn = connect(":memory:")
    migrate(conn)
    seed_listing_observed_on(conn, listing_id=1, title="Acrylic Print 16x24", day=AS_OF, views=10)
    seed_sale_observed(
        conn, receipt_id=1, day=AS_OF, transactions=[(1, 1, 1, 87.0)], shipping_tax_usd=28.0
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    r = analytics.revenue_window(conn, USER_ID, cfg, as_of=AS_OF)
    sellers = analytics.top_sellers(conn, USER_ID, cfg, as_of=AS_OF)
    assert r.current_usd == pytest.approx(115.0)  # 87 item + 28 shipping/tax
    assert sellers[0].revenue_usd == pytest.approx(87.0)  # item price only


def test_top_sellers_only_the_listing_that_sold(shop, cfg):
    sellers = analytics.top_sellers(shop, USER_ID, cfg, as_of=AS_OF)
    assert [s.listing_id for s in sellers] == [LISTING_SELLER]
    assert sellers[0].units == 2
    assert sellers[0].revenue_usd == pytest.approx(174.0)


def test_viewed_not_sold_identifies_the_right_listing(shop, cfg):
    result = analytics.viewed_not_sold(shop, USER_ID)
    assert [v.listing_id for v in result] == [LISTING_VIEWED_NOT_SOLD]
    assert result[0].views_lifetime > 0


def test_dead_listing_identified_correctly(shop, cfg):
    dead = analytics.dead_listings(shop, USER_ID, cfg, as_of=AS_OF)
    assert [d.listing_id for d in dead] == [LISTING_DEAD]
    assert dead[0].views_in_window == 0
    assert dead[0].days_observed >= cfg.dead_listing.min_observed_days


def test_seasonal_listing_is_not_flagged_dead(shop, cfg):
    # design §17.12's exact caveat: a listing quiet most of the year must
    # not be flagged dead when its most recent burst of activity is still
    # inside the configured dead_listing.window_days.
    dead_ids = {d.listing_id for d in analytics.dead_listings(shop, USER_ID, cfg, as_of=AS_OF)}
    assert LISTING_SEASONAL not in dead_ids


def test_seller_and_viewed_not_sold_are_mutually_exclusive_from_dead(shop, cfg):
    dead_ids = {d.listing_id for d in analytics.dead_listings(shop, USER_ID, cfg, as_of=AS_OF)}
    assert LISTING_SELLER not in dead_ids
    assert LISTING_VIEWED_NOT_SOLD not in dead_ids


def test_trending_flags_the_accelerating_listing_only(shop, cfg):
    trend = analytics.trending(shop, USER_ID, cfg, as_of=AS_OF)
    assert [t.listing_id for t in trend] == [LISTING_SELLER]
    assert trend[0].views_recent > trend[0].views_prior


def test_viewed_not_sold_listing_does_not_trend(shop, cfg):
    # steady (non-accelerating) growth must not be reported as trending.
    trend_ids = {t.listing_id for t in analytics.trending(shop, USER_ID, cfg, as_of=AS_OF)}
    assert LISTING_VIEWED_NOT_SOLD not in trend_ids


def test_product_type_breakdown_groups_acrylic_and_catalog_counts(shop, cfg):
    breakdown = analytics.product_type_breakdown(shop, USER_ID, cfg, as_of=AS_OF)
    by_type = {b.product_type: b for b in breakdown}
    assert {"acrylic", "poster", "canvas", "unknown"} <= by_type.keys()
    assert by_type["acrylic"].units == 2
    assert by_type["acrylic"].revenue_usd == pytest.approx(174.0)
    assert by_type["acrylic"].listing_count == 1
    # nothing else sold this window -- but the catalog still counts them
    assert by_type["poster"].revenue_usd == 0.0
    assert by_type["poster"].listing_count == 1
    assert by_type["canvas"].listing_count == 1
    assert by_type["unknown"].listing_count == 1


def test_size_breakdown_extracts_from_title_when_present(shop, cfg):
    sizes = analytics.size_breakdown(shop, USER_ID, cfg, as_of=AS_OF)
    assert len(sizes) == 1
    assert sizes[0].size == "16x24"
    assert sizes[0].units == 2
    assert sizes[0].revenue_usd == pytest.approx(174.0)


def test_size_breakdown_reports_none_when_no_signal_in_title():
    # isolated, minimal scenario: a sale whose title has no size pattern.
    conn = connect(":memory:")
    migrate(conn)
    from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed

    seed_listing_observed_on(
        conn, listing_id=1, title="Foggy Pines Fine Art Poster", day=AS_OF, views=10
    )
    seed_sale_observed(conn, receipt_id=1, day=AS_OF, transactions=[(1, 1, 1, 19.0)])
    rebuild_core(conn)
    rebuild_ops(conn)

    sizes = analytics.size_breakdown(conn, USER_ID, load_ops_config(), as_of=AS_OF)
    assert len(sizes) == 1
    assert sizes[0].size is None
    assert sizes[0].units == 1


def test_data_quality_notes_flag_missing_size_signal():
    conn = connect(":memory:")
    migrate(conn)
    from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed

    seed_listing_observed_on(
        conn, listing_id=1, title="Foggy Pines Fine Art Poster", day=AS_OF, views=10
    )
    seed_sale_observed(conn, receipt_id=1, day=AS_OF, transactions=[(1, 1, 1, 19.0)])
    rebuild_core(conn)
    rebuild_ops(conn)

    notes = analytics.data_quality_notes(conn, USER_ID, load_ops_config(), as_of=AS_OF)
    assert any("floor on" in n for n in notes)


# --- F1: "no observations" vs "confirmed zero" must never collapse --------


def test_dead_listing_with_zero_observations_in_window_is_not_confirmed_dead():
    # reviewer repro 1: one observation 400 days ago, none since. The window
    # (180d) sees ZERO rows -- that is an absence of data, not a confirmed
    # flat view count, and must not be reported as "0 views in the window".
    from datetime import timedelta

    conn = connect(":memory:")
    migrate(conn)
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    cfg = load_ops_config()
    seed_listing_observed_on(
        conn, listing_id=1, title="Old Listing", day=AS_OF - timedelta(days=400), views=10
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    assert analytics.dead_listings(conn, USER_ID, cfg, as_of=AS_OF) == []
    notes = analytics.data_quality_notes(conn, USER_ID, cfg, as_of=AS_OF)
    assert any("no confirmed view reading" in n for n in notes)


def test_dead_listing_with_one_observation_in_window_is_not_confirmed_dead():
    # reviewer repro 2: observations at -300d (views=10) and -3d (views=50000)
    # -- exactly one row falls inside the 180d window, so no delta can be
    # computed from it alone. Must NOT be reported as "0 views", regardless
    # of how large the (unmeasured, from this window's rows alone) gain was.
    from datetime import timedelta

    conn = connect(":memory:")
    migrate(conn)
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    cfg = load_ops_config()
    seed_listing_observed_on(
        conn, listing_id=1, title="Spiky Listing", day=AS_OF - timedelta(days=300), views=10
    )
    seed_listing_observed_on(
        conn, listing_id=1, title="Spiky Listing", day=AS_OF - timedelta(days=3), views=50000
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    assert analytics.dead_listings(conn, USER_ID, cfg, as_of=AS_OF) == []
    notes = analytics.data_quality_notes(conn, USER_ID, cfg, as_of=AS_OF)
    assert any("no confirmed view reading" in n for n in notes)


def test_a_catalog_wide_sync_gap_does_not_declare_the_whole_catalog_dead(shop, cfg):
    # reviewer's systemic case: one sync after a long gap must not fold
    # "nothing observed" into "confirmed dead" for every listing at once.
    from datetime import timedelta

    far_future = AS_OF + timedelta(days=200)  # past every seeded observation
    assert analytics.dead_listings(shop, USER_ID, cfg, as_of=far_future) == []
    notes = analytics.data_quality_notes(shop, USER_ID, cfg, as_of=far_future)
    assert any("4 listing(s)" in n for n in notes)


def test_shoot_more_surfaces_only_the_underrepresented_seller(shop, cfg):
    suggestions = analytics.shoot_more(shop, USER_ID, cfg, as_of=AS_OF)
    assert [s.product_type for s in suggestions] == ["acrylic"]
    assert suggestions[0].listing_count == 1


def test_shoot_more_never_suggests_unknown(shop, cfg):
    suggestions = analytics.shoot_more(shop, USER_ID, cfg, as_of=AS_OF)
    assert "unknown" not in {s.product_type for s in suggestions}


def test_analytics_functions_never_write_events(shop, cfg):
    from shopsteward.core.events import read_all

    before = len(read_all(shop))
    analytics.revenue_window(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.top_sellers(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.viewed_not_sold(shop, USER_ID)
    analytics.dead_listings(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.trending(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.product_type_breakdown(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.size_breakdown(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.shoot_more(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.data_quality_notes(shop, USER_ID, cfg, as_of=AS_OF)
    analytics.proven_listings(shop, USER_ID, cfg, as_of=AS_OF)
    after = len(read_all(shop))
    assert before == after


# --- T12 (operator-approved 2026-08-25 /autoplan gate): proven_listings() --
# widened capability-GATING eligibility, distinct from top_sellers()'s
# brief-facing 7-day window -------------------------------------------------


def test_proven_listings_includes_a_sale_45_days_ago_the_7d_gate_would_miss(cfg):
    conn = connect(":memory:")
    migrate(conn)
    seed_listing_observed_on(
        conn, listing_id=501, title="Aspen Grove Print", day=AS_OF - timedelta(days=45), views=50
    )
    seed_sale_observed(
        conn,
        receipt_id=1,
        day=AS_OF - timedelta(days=45),
        transactions=[(501, 1, 2, 50.0)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    assert analytics.top_sellers(conn, USER_ID, cfg, as_of=AS_OF) == []
    proven = analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF)
    assert [p.listing_id for p in proven] == [501]
    assert proven[0].units == 2
    assert proven[0].revenue_usd == pytest.approx(100.0)


def test_proven_listings_includes_a_200_day_old_sale_via_the_lifetime_arm(cfg):
    conn = connect(":memory:")
    migrate(conn)
    seed_listing_observed_on(
        conn, listing_id=502, title="Old Barn Print", day=AS_OF - timedelta(days=200), views=30
    )
    seed_sale_observed(
        conn,
        receipt_id=2,
        day=AS_OF - timedelta(days=200),
        transactions=[(502, 2, 1, 60.0)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    proven = analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF)
    assert [p.listing_id for p in proven] == [502]
    assert proven[0].units == 1
    assert proven[0].revenue_usd == pytest.approx(60.0)


def test_proven_listings_excludes_zero_sales_no_view_history(cfg):
    conn = connect(":memory:")
    migrate(conn)
    seed_listing_observed_on(conn, listing_id=503, title="Never Sold", day=AS_OF, views=10)
    rebuild_core(conn)
    rebuild_ops(conn)

    assert analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF) == []


def test_proven_listings_views_velocity_arm_includes_a_rising_zero_sales_listing(cfg):
    # delta=10, >= default min_delta=5
    conn = connect(":memory:")
    migrate(conn)
    for offset, views in ((-29, 10), (0, 20)):
        seed_listing_observed_on(
            conn,
            listing_id=504,
            title="Rising Listing",
            day=AS_OF + timedelta(days=offset),
            views=views,
        )
    rebuild_core(conn)
    rebuild_ops(conn)

    proven = analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF)
    assert [p.listing_id for p in proven] == [504]
    assert proven[0].units == 0
    assert proven[0].revenue_usd == 0.0


def test_proven_listings_views_velocity_arm_excludes_growth_below_the_bar(cfg):
    conn = connect(":memory:")
    migrate(conn)
    for offset, views in ((-29, 10), (0, 12)):  # delta=2, < default min_delta=5
        seed_listing_observed_on(
            conn,
            listing_id=505,
            title="Barely Moving",
            day=AS_OF + timedelta(days=offset),
            views=views,
        )
    rebuild_core(conn)
    rebuild_ops(conn)

    assert analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF) == []


def test_proven_listings_views_velocity_arm_excludes_a_single_observation():
    # _views_delta's own convention: fewer than two observations in the
    # window is UNMEASURABLE, not zero growth -- must never read as eligible.
    conn = connect(":memory:")
    migrate(conn)
    seed_listing_observed_on(conn, listing_id=506, title="One Reading Only", day=AS_OF, views=1000)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = load_ops_config()

    assert analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF) == []


def test_data_quality_notes_reports_unmeasurable_views_velocity_count(cfg):
    """L11 (guardrail review, 2026-08-25): a zero-sales listing with a
    single view observation in the window is UNMEASURABLE for
    `proven_listings()`'s views-velocity arm (c) -- excluded silently
    there, but `data_quality_notes()` must say so explicitly, distinct
    from the dead-listing note (a different window, a different check)."""
    conn = connect(":memory:")
    migrate(conn)
    seed_listing_observed_on(conn, listing_id=701, title="One Reading Only", day=AS_OF, views=1000)
    rebuild_core(conn)
    rebuild_ops(conn)

    assert analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF) == []
    notes = analytics.data_quality_notes(conn, USER_ID, cfg, as_of=AS_OF)
    matches = [n for n in notes if "views-velocity arm" in n]
    assert len(matches) == 1
    assert "1 zero-sales listing(s)" in matches[0]


def test_proven_listings_limit_keeps_highest_revenue_rows_default_unbounded(cfg):
    """M4 (guardrail review, 2026-08-25): `limit=None` (the default) is
    unbounded -- the capability `_candidates()` grounding call sites need
    every real target, never a token-cost truncation. A caller that DOES
    pass `limit` (planner.py's LLM-facing facts block) gets the
    highest-revenue rows first, per the function's own sort order."""
    conn = connect(":memory:")
    migrate(conn)
    for listing_id, units, revenue in ((601, 1, 30.0), (602, 2, 90.0), (603, 3, 60.0)):
        seed_listing_observed_on(
            conn, listing_id=listing_id, title=f"L{listing_id}", day=AS_OF, views=10
        )
        seed_sale_observed(
            conn,
            receipt_id=listing_id,
            day=AS_OF,
            transactions=[(listing_id, listing_id, units, revenue / units)],
        )
    rebuild_core(conn)
    rebuild_ops(conn)

    unbounded = analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF)
    assert {p.listing_id for p in unbounded} == {601, 602, 603}

    bounded = analytics.proven_listings(conn, USER_ID, cfg, as_of=AS_OF, limit=2)
    assert [p.listing_id for p in bounded] == [602, 603]  # highest revenue first, 601 dropped


def test_proof_phrase_distinguishes_sales_proof_from_velocity_proof():
    from shopsteward.pipeline.ops.models import ListingSales

    sold = ListingSales(listing_id=1, title="x", units=3, revenue_usd=10.0)
    rising = ListingSales(listing_id=2, title="y", units=0, revenue_usd=0.0)
    assert analytics.proof_phrase(sold) == "top seller (3 sold)"
    assert analytics.proof_phrase(rising) == "rising views, no sales yet"
