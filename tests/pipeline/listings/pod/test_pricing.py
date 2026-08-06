import pytest

from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod import pricing as pod_pricing
from shopsteward.pipeline.listings.pod.models import PodConfig
from shopsteward.pipeline.listings.pricing import BelowFloor

LISTING_RULES = listing_config.load_listing_config().pricing  # real listing.json etsy_fees


def _pod_pricing(**overrides: object) -> object:
    base = dict(
        markup=3.0,
        price_ending=0.0,
        margin_floor_abs=8.0,
        margin_floor_pct=0.25,
        max_price=300.0,
        shipping_included=True,
    )
    base.update(overrides)
    return PodConfig.model_validate(
        {
            "schema": "shopsteward.pod/1",
            "name": "default",
            "enabled": True,
            "region": "US",
            "currency": "USD",
            "print_file": {
                "prefer": "tiff_master",
                "max_bytes": 100_000_000,
                "min_dpi": 150,
                "aspect_tolerance": 0.02,
                "host_ttl_seconds": 86400,
            },
            "aspects": {"2:3": 1.5},
            "formats_by_aspect": {},
            "routing": [],
            "catalog": {},
            "costs_verified_on": "1970-01-01",
            "cost_staleness_days": 90,
            "pricing": base,
            "copy": {"title_suffix": {}, "description_block": {}},
            "images": {"max_ours": 5, "hard_cap": 10, "trim_provider_images": True},
            "link_timeout_seconds": 180,
            "link_poll_interval_seconds": 10,
        }
    ).pricing


# --- round_up_to_ending -----------------------------------------------------


def test_round_up_to_whole_dollar():
    assert pod_pricing.round_up_to_ending(9.337, 0.0) == 10.0


def test_round_up_is_a_noop_when_already_exact():
    assert pod_pricing.round_up_to_ending(10.0, 0.0) == 10.0


def test_round_up_to_ninety_nine_cent_ending():
    assert pod_pricing.round_up_to_ending(9.02, 0.99) == 9.99
    assert pod_pricing.round_up_to_ending(10.00, 0.99) == 10.99
    assert pod_pricing.round_up_to_ending(9.99, 0.99) == 9.99


def test_round_up_to_ending_rejects_out_of_range_ending():
    with pytest.raises(ValueError, match="price_ending"):
        pod_pricing.round_up_to_ending(10.0, 1.0)


# --- retail_price ------------------------------------------------------------


def test_retail_price_low_cost_is_driven_by_the_absolute_floor():
    # markup*0 == 0, so the $8 absolute floor sets the price, not markup.
    rules = _pod_pricing(markup=3.0, margin_floor_abs=8.0, margin_floor_pct=0.25)
    price = pod_pricing.retail_price(0.0, rules, LISTING_RULES)
    assert price == 10.0  # p_abs=9.337.. rounded up to the next whole dollar
    pod_pricing.enforce_floor(price, 0.0, rules, LISTING_RULES)  # must not raise


def test_retail_price_high_cost_low_markup_is_driven_by_the_percentage_floor():
    rules = _pod_pricing(markup=1.0, margin_floor_abs=8.0, margin_floor_pct=0.25)
    price = pod_pricing.retail_price(100.0, rules, LISTING_RULES)
    assert price == 154.0  # p_pct=153.36.. rounded up
    pod_pricing.enforce_floor(price, 100.0, rules, LISTING_RULES)  # must not raise


def test_retail_price_high_markup_is_driven_by_markup_itself():
    rules = _pod_pricing(markup=3.0, margin_floor_abs=8.0, margin_floor_pct=0.25)
    price = pod_pricing.retail_price(43.53, rules, LISTING_RULES)
    assert price == 131.0  # 43.53*3 = 130.59, both floors clear well below that
    pod_pricing.enforce_floor(price, 43.53, rules, LISTING_RULES)


def test_retail_price_respects_a_nonzero_price_ending():
    rules = _pod_pricing(markup=3.0, price_ending=0.99)
    price = pod_pricing.retail_price(0.0, rules, LISTING_RULES)
    assert price == 9.99


# --- enforce_floor -----------------------------------------------------------


def test_enforce_floor_raises_below_absolute_floor():
    rules = _pod_pricing(margin_floor_abs=8.0, margin_floor_pct=0.0)
    with pytest.raises(BelowFloor):
        pod_pricing.enforce_floor(1.00, 0.0, rules, LISTING_RULES)


def test_enforce_floor_raises_below_percentage_floor():
    rules = _pod_pricing(margin_floor_abs=0.0, margin_floor_pct=0.90)
    with pytest.raises(BelowFloor):
        pod_pricing.enforce_floor(10.00, 0.0, rules, LISTING_RULES)


def test_enforce_floor_accepts_an_operator_override_that_clears_both_floors():
    # PRICING DECISION 2026-08-04: an override is validated the same way an
    # auto-solved price is -- this is the acrylic_16x24 shipped override.
    rules = _pod_pricing(margin_floor_abs=8.0, margin_floor_pct=0.25)
    pod_pricing.enforce_floor(149.0, 43.53, rules, LISTING_RULES)  # must not raise


def test_enforce_floor_rejects_an_override_below_the_floor_loudly():
    rules = _pod_pricing(margin_floor_abs=8.0, margin_floor_pct=0.25)
    with pytest.raises(BelowFloor):
        pod_pricing.enforce_floor(44.00, 43.53, rules, LISTING_RULES)  # $0.47 profit, way under $8


# --- pod_economics -----------------------------------------------------------


def test_pod_economics_matches_hand_calculation():
    # price=149, unit_cost=43.53: fees=0.20+149*0.095+0.25=14.605 -> rounds to 14.61
    # net=149-14.61-43.53=90.86 (compute_economics rounds fees/net to cents)
    econ = pod_pricing.pod_economics(149.0, 43.53, LISTING_RULES)
    assert econ.etsy_fees == pytest.approx(14.61, abs=1e-2)
    assert econ.net == pytest.approx(90.86, abs=1e-2)
    assert econ.margin_pct == pytest.approx(0.6099, abs=1e-3)


# --- closed-form solve against the REAL shipped catalog ----------------------
# Every shipped variant currently carries a retail_override, so the
# markup/floor solve above is otherwise only exercised by synthetic costs.
# This runs it against every real base_cost with the override ignored, so a
# bug in the closed form itself would surface without shipping a 12th SKU.


def test_retail_price_clears_both_floors_for_every_shipped_variant_real_cost():
    cfg = pod_config.load_pod_config()
    for provider_catalog in cfg.catalog.values():
        for product in provider_catalog.products.values():
            for variant in product.variants:
                unit_cost = variant.base_cost + (
                    variant.shipping_est if cfg.pricing.shipping_included else 0.0
                )
                price = pod_pricing.retail_price(unit_cost, cfg.pricing, LISTING_RULES)
                pod_pricing.enforce_floor(price, unit_cost, cfg.pricing, LISTING_RULES)  # not raise
                assert price >= unit_cost * cfg.pricing.markup - 1e-6
