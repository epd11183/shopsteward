"""PURE POD pricing (design §5): no DB/event I/O. The Etsy fee model is read
from listing.json (`PricingRules.etsy_fees`, listings/pricing.py's
`compute_economics`) rather than duplicated here -- pod.json only supplies
the POD-specific knobs (markup, both margin floors, price_ending, max_price).

`retail_price` is the closed-form solve design §5 spells out: the price that
satisfies BOTH margin floors simultaneously, by construction, is
`max(cost*markup, p_abs, p_pct)` rounded up to `price_ending` -- so
`enforce_floor` can never fail on that path; it's still called afterwards as
a guard (a config bug in the closed form itself would otherwise ship
silently). PRICING DECISION 2026-08-04 lets an operator bypass the solve
entirely with `PodCatalogVariant.retail_override`; `enforce_floor` is what
makes a below-floor override fail LOUDLY (a `BelowFloor` raise) instead of
silently discounting -- pod/build.py never catches it.
"""

import math

from shopsteward.pipeline.listings.models import Economics, PricingRules
from shopsteward.pipeline.listings.pod.models import _PodPricing
from shopsteward.pipeline.listings.pricing import BelowFloor, compute_economics

__all__ = ["enforce_floor", "pod_economics", "retail_price", "round_up_to_ending"]

_EPSILON = 1e-6


def round_up_to_ending(price: float, ending: float) -> float:
    """Round `price` UP to the nearest whole-dollar-plus-`ending` amount
    (e.g. ending=0.00 -> next whole dollar; ending=0.99 -> next X.99). Never
    rounds down -- a floor-satisfying price must not become a floor-violating
    one by rounding."""
    if not (0.0 <= ending < 1.0):
        raise ValueError(f"price_ending must be in [0, 1); got {ending}")
    base = math.floor(price)
    candidate = round(base + ending, 2)
    if candidate < price - _EPSILON:
        candidate = round(candidate + 1, 2)
    return candidate


def retail_price(
    unit_cost: float, pod_pricing: _PodPricing, listing_pricing: PricingRules
) -> float:
    """Design §5's closed form: retail = round_up_to_ending(max(p_markup,
    p_abs, p_pct)), where p_abs/p_pct are the prices that exactly satisfy
    the absolute/percentage margin floor given the Etsy fee model
    (`f(p) = listing_fee + p*(transaction_pct+payment_pct) + payment_flat`).
    """
    fees = listing_pricing.etsy_fees
    t_plus_pp = fees.transaction_pct + fees.payment_pct
    denom = 1 - t_plus_pp
    if denom <= 0 or denom - pod_pricing.margin_floor_pct <= 0:
        raise ValueError(
            "transaction_pct + payment_pct + margin_floor_pct must be < 1 "
            "for the percentage-floor closed form to have a finite solution"
        )

    p_markup = unit_cost * pod_pricing.markup
    p_abs = (
        pod_pricing.margin_floor_abs + unit_cost + fees.listing_fee + fees.payment_flat
    ) / denom
    p_pct = (unit_cost + fees.listing_fee + fees.payment_flat) / (
        denom - pod_pricing.margin_floor_pct
    )
    return round_up_to_ending(max(p_markup, p_abs, p_pct), pod_pricing.price_ending)


def pod_economics(price: float, unit_cost: float, listing_pricing: PricingRules) -> Economics:
    """Thin wrapper over listings/pricing.py's compute_economics so callers
    in this package never need to import from listings.pricing directly for
    the common case -- one entry point for "what does this price net after
    Etsy fees and provider cost"."""
    return compute_economics(price, listing_pricing, unit_cost)


def enforce_floor(
    price: float, unit_cost: float, pod_pricing: _PodPricing, listing_pricing: PricingRules
) -> None:
    """Asserts `price` clears BOTH margin floors against `unit_cost` -- run
    unconditionally on every priced variant, auto-solved or overridden
    (design §5, PRICING DECISION 2026-08-04). Checks the actual net/margin_pct
    computed at `price`, not a re-derivation of p_abs/p_pct, so it stays
    correct even if `price` came from an operator override rather than the
    closed form above."""
    econ = pod_economics(price, unit_cost, listing_pricing)
    if econ.net < pod_pricing.margin_floor_abs - _EPSILON:
        raise BelowFloor(
            f"price {price:.2f} nets {econ.net:.2f}, below the "
            f"{pod_pricing.margin_floor_abs:.2f} absolute margin floor"
        )
    if price > 0 and econ.margin_pct < pod_pricing.margin_floor_pct - _EPSILON:
        raise BelowFloor(
            f"price {price:.2f} margins {econ.margin_pct:.2%}, below the "
            f"{pod_pricing.margin_floor_pct:.2%} percentage margin floor"
        )
