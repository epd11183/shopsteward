"""PURE pricing + economics (design §2): no DB/event I/O. apply_price/
enforce_floor/compute_economics take and return plain values so tests
exercise them without a DB; the orchestrator (drafts.py) is the only caller
that appends listingdraft.priced."""

from shopsteward.pipeline.listings.models import Economics, PricingRules


class BelowFloor(Exception):
    """Raised when a price falls below the format's configured margin_floor."""


def apply_price(format: str, rules: PricingRules) -> float:
    return rules.formats[format].base_price


def enforce_floor(price: float, format: str, rules: PricingRules) -> None:
    floor = rules.formats[format].margin_floor
    if price < floor:
        raise BelowFloor(
            f"price {price:.2f} is below the {floor:.2f} margin floor for format {format!r}"
        )


def compute_economics(price: float, rules: PricingRules, unit_cost: float = 0.0) -> Economics:
    """Etsy fee model (rates from config/defaults/listing.json pricing.etsy_fees,
    never hardcoded): a flat listing fee, a percentage transaction fee, and a
    percentage-plus-flat payment-processing fee.

    `unit_cost` (M5b addition, design §5) subtracts a physical SKU's provider
    cost from net -- M5a's digital call sites are unchanged, since a digital
    download has no provider cost and the default is 0.0. margin_pct =
    net/price (0.0 at price<=0, never a ZeroDivisionError)."""
    fees = rules.etsy_fees
    transaction_fee = price * fees.transaction_pct
    payment_fee = price * fees.payment_pct + fees.payment_flat
    total_fees = round(fees.listing_fee + transaction_fee + payment_fee, 2)
    net = round(price - total_fees - unit_cost, 2)
    margin_pct = round(net / price, 4) if price > 0 else 0.0
    return Economics(price=price, etsy_fees=total_fees, net=net, margin_pct=margin_pct)
