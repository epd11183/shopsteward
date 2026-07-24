import pytest

from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.pricing import (
    BelowFloor,
    apply_price,
    compute_economics,
    enforce_floor,
)

CFG = listing_config.load_listing_config()
RULES = CFG.pricing


def test_apply_price_reads_base_price_from_config():
    assert apply_price("digital_download", RULES) == 12.00


def test_enforce_floor_passes_at_or_above_floor():
    enforce_floor(6.00, "digital_download", RULES)  # == floor, should not raise
    enforce_floor(12.00, "digital_download", RULES)


def test_enforce_floor_raises_below_floor():
    with pytest.raises(BelowFloor):
        enforce_floor(5.99, "digital_download", RULES)


def test_compute_economics_matches_hand_calculation():
    # price=12.00; listing_fee=0.20; transaction=12*0.065=0.78;
    # payment=12*0.03+0.25=0.61; total_fees=1.59; net=10.41
    econ = compute_economics(12.00, RULES)
    assert econ.price == 12.00
    assert econ.etsy_fees == pytest.approx(1.59)
    assert econ.net == pytest.approx(10.41)


def test_compute_economics_at_a_different_price():
    # price=20.00; transaction=20*0.065=1.30; payment=20*0.03+0.25=0.85;
    # total_fees=0.20+1.30+0.85=2.35; net=17.65
    econ = compute_economics(20.00, RULES)
    assert econ.etsy_fees == pytest.approx(2.35)
    assert econ.net == pytest.approx(17.65)
