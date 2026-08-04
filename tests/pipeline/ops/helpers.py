"""Shared synthetic event-log builders for ops tests -- plain importable
helpers, not a conftest.py fixture module (listings/helpers.py precedent).

etsy.listing.observed is inserted with an explicit `created_at` (bypassing
core.events.append()'s DB-default timestamp) because
ops/projections.py buckets proj_listing_daily on the EVENT's own
created_at date -- tests need to place an observation on a specific day two
years in the past. This is still a plain INSERT, never an UPDATE/DELETE, so
append-only is preserved."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from shopsteward.core.events import Event, append

USER_ID = 1

# --- the shared ~2-year, 4-listing scenario (test_analytics.py, test_brief.py,
# test_cli.py) -- one listing per design test category (design §11's
# instruction: seller, viewed-but-never-sold, dead, seasonal). AS_OF is a
# fixed anchor, not wall-clock "today", so the scenario is reproducible.

AS_OF = date(2026, 2, 1)
ANCHOR = AS_OF - timedelta(days=730)  # "the shop has been live ~2 years"

LISTING_SELLER = 111  # sells this window AND is trending
LISTING_VIEWED_NOT_SOLD = 222
LISTING_DEAD = 333
LISTING_SEASONAL = 444  # quiet most of the year; NOT dead (Dec spike is inside the 180d window)


def _money(usd: float) -> dict:
    return {"amount": round(usd * 100), "divisor": 100, "currency_code": "USD"}


def seed_listing_observed_on(
    conn: sqlite3.Connection,
    *,
    listing_id: int,
    title: str,
    day: date,
    views: int,
    num_favorers: int = 0,
    price_usd: float = 87.00,
    state: str = "active",
    user_id: int = USER_ID,
) -> None:
    payload = {
        "listing_id": listing_id,
        "title": title,
        "state": state,
        "quantity": 999,
        "views": views,
        "num_favorers": num_favorers,
        "price": _money(price_usd),
        "tags": [],
    }
    created_at = f"{day.isoformat()}T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (user_id, "etsy.listing.observed", json.dumps(payload), created_at),
    )
    conn.commit()


def seed_sale_observed(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    day: date,
    transactions: list[tuple[int, int, int, float]],  # (listing_id, transaction_id, qty, price_usd)
    shipping_tax_usd: float = 0.0,
    user_id: int = USER_ID,
) -> None:
    # shipping_tax_usd defaults to 0 for every existing call site (no change
    # to their numbers), but a real Etsy receipt's grandtotal is item price
    # PLUS shipping/tax -- callers exercising the F2 reconciliation gap
    # (revenue_window sums grandtotal; top_sellers/product_type_breakdown
    # sum item price only) must pass a nonzero value, or this fixture just
    # verifies the reader against itself.
    created_timestamp = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    total = sum(qty * price for _, _, qty, price in transactions) + shipping_tax_usd
    append(
        conn,
        Event(
            user_id=user_id,
            type="etsy.sale.observed",
            payload={
                "receipt_id": receipt_id,
                "created_timestamp": created_timestamp,
                "grandtotal": _money(total),
                "transactions": [
                    {
                        "transaction_id": transaction_id,
                        "listing_id": listing_id,
                        "quantity": qty,
                        "price": _money(price_usd),
                    }
                    for listing_id, transaction_id, qty, price_usd in transactions
                ],
            },
        ),
    )


def seed_two_year_shop(conn: sqlite3.Connection) -> None:
    """Seeds all events in day-ascending order (so sqlite's autoincrement id
    order matches created_at order, matching real sync behaviour) so
    core.projections.rebuild()'s INSERT OR REPLACE ends up holding each
    listing's most-recent observation, exactly as a real nightly sync would.

    111 (seller/trending): steady growth, accelerating in the most recent 7d,
        two sales this window ($174) and one sale in the prior window ($87)
        -- revenue +100% window-over-window.
    222 (viewed_not_sold): steady (non-accelerating) view growth, zero sales
        ever.
    333 (dead): flat views and zero sales for the full 180d dead-listing
        window, despite one sale 400 days ago (i.e. it USED to sell).
    444 (seasonal): a view+sale spike ~60 days ago (inside the 180d dead
        window, so correctly NOT flagged dead) but silent this week -- the
        "Winter listings look dead in July" case from the design's §17.12.
    """
    # anchors, ~2 years ago -- establishes days_observed >= min_observed_days
    # for every listing with a single old row each.
    for listing_id, title, views in (
        (LISTING_SELLER, "Sandhill Cranes at Dawn Acrylic Print 16x24", 500),
        (LISTING_VIEWED_NOT_SOLD, "Foggy Pines Fine Art Poster", 50),
        (LISTING_DEAD, "Red Rock Canyon Fine Art Print", 20),
        (LISTING_SEASONAL, "Winter Wonderland Canvas Print", 10),
    ):
        seed_listing_observed_on(conn, listing_id=listing_id, title=title, day=ANCHOR, views=views)

    # 111: dense 20-day stretch, accelerating in the most recent 7 days.
    views_111 = {
        -19: 1000,
        -18: 1001,
        -17: 1002,
        -16: 1003,
        -15: 1004,
        -14: 1005,
        -13: 1006,
        -12: 1008,
        -11: 1010,
        -10: 1012,
        -9: 1014,
        -8: 1016,
        -7: 1018,
        -6: 1020,
        -5: 1030,
        -4: 1040,
        -3: 1050,
        -2: 1060,
        -1: 1070,
        0: 1080,
    }
    for offset in range(-19, 1):
        seed_listing_observed_on(
            conn,
            listing_id=LISTING_SELLER,
            title="Sandhill Cranes at Dawn Acrylic Print 16x24",
            day=AS_OF + timedelta(days=offset),
            views=views_111[offset],
        )
    seed_sale_observed(
        conn,
        receipt_id=9001,
        day=AS_OF + timedelta(days=-10),
        transactions=[(LISTING_SELLER, 90011, 1, 87.00)],
    )
    seed_sale_observed(
        conn,
        receipt_id=9002,
        day=AS_OF + timedelta(days=-5),
        transactions=[(LISTING_SELLER, 90021, 1, 87.00)],
    )
    seed_sale_observed(
        conn,
        receipt_id=9003,
        day=AS_OF + timedelta(days=-2),
        transactions=[(LISTING_SELLER, 90031, 1, 87.00)],
    )

    # 222: steady, non-accelerating growth (+2/day) -- viewed, never sold.
    for offset in range(-19, 1):
        seed_listing_observed_on(
            conn,
            listing_id=LISTING_VIEWED_NOT_SOLD,
            title="Foggy Pines Fine Art Poster",
            day=AS_OF + timedelta(days=offset),
            views=300 + 2 * (offset + 19),
        )

    # 333: flat views inside the 180d dead window; a sale exists but it's
    # 400 days old, well outside every window this slice looks at.
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_DEAD,
        title="Red Rock Canyon Fine Art Print",
        day=AS_OF + timedelta(days=-170),
        views=45,
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_DEAD,
        title="Red Rock Canyon Fine Art Print",
        day=AS_OF + timedelta(days=-7),
        views=45,
    )
    seed_sale_observed(
        conn,
        receipt_id=9004,
        day=AS_OF + timedelta(days=-400),
        transactions=[(LISTING_DEAD, 90041, 1, 40.00)],
    )

    # 444: a spike 60 days ago (inside the 180d dead window), silent since.
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_SEASONAL,
        title="Winter Wonderland Canvas Print",
        day=AS_OF + timedelta(days=-65),
        views=10,
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_SEASONAL,
        title="Winter Wonderland Canvas Print",
        day=AS_OF + timedelta(days=-60),
        views=500,
    )
    seed_sale_observed(
        conn,
        receipt_id=9005,
        day=AS_OF + timedelta(days=-60),
        transactions=[(LISTING_SEASONAL, 90051, 1, 65.00)],
    )
