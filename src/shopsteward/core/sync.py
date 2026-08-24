"""Pull an Etsy snapshot through the adapter and append observation events."""

import sqlite3

from pydantic import BaseModel

from shopsteward.adapters.etsy.auth import EtsyTokenStore
from shopsteward.adapters.etsy.interface import EtsyAdapter
from shopsteward.core.events import Event, append, read_all


class SyncResult(BaseModel):
    shops: int = 0
    listings: int = 0
    listing_images: int = 0
    receipts: int = 0


def read_live_observed(
    conn: sqlite3.Connection,
    type_prefix: str,
    *,
    token_store: EtsyTokenStore | None = None,
) -> list[Event]:
    """Read `etsy.listing.observed` / `etsy.sale.observed` events, excluding
    any rows that predate the FIRST (earliest) `etsy.shop.observed` for the
    shop currently configured in the Etsy token store.

    The anchor is the earliest match, not the latest: `sync --live` appends a
    fresh `etsy.shop.observed` event on every call, so a shop's identity
    anchor is a one-time boundary (fixture-era junk vs. this shop's real
    history), not a moving cursor. Anchoring on the latest shop.observed
    would silently advance forward on every subsequent sync and orphan
    listing/sale events from every earlier *real* sync too -- not just the
    original fixture pollution.

    Guards against a dev DB polluted by a `--fixtures` sync (tiny synthetic
    shop/listing/receipt ids) that predates a later real `--live` sync
    against the operator's actual shop: those fixture rows have no shop_id
    field of their own to filter by, but they necessarily precede the real
    shop's first `etsy.shop.observed` anchor in row-id order.

    Falls back to returning everything unfiltered when there's nothing to
    anchor against yet -- no stored Etsy auth (most of the test suite), or
    no `etsy.shop.observed` event for the configured shop (a fresh DB, or
    fixtures seeded directly via `append()` without a shop.observed event).
    This is a data-hygiene filter, not an auth gate.
    """
    events = read_all(conn, type_prefix)

    store = token_store if token_store is not None else EtsyTokenStore()
    tokens = store.load()
    if tokens is None or tokens.shop_id is None:
        return events
    shop_id = tokens.shop_id

    anchor_id: int | None = None
    for e in read_all(conn, "etsy.shop.observed"):
        if e.payload.get("shop_id") == shop_id:
            anchor_id = e.id  # read_all is id-ordered, so the first match wins
            break
    if anchor_id is None:
        return events

    return [e for e in events if e.id is not None and e.id >= anchor_id]


def _sale_events_for_user(conn: sqlite3.Connection, user_id: int) -> list[Event]:
    return [e for e in read_all(conn, "etsy.sale.observed") if e.user_id == user_id]


def sync_etsy(conn: sqlite3.Connection, adapter: EtsyAdapter, user_id: int) -> SyncResult:
    result = SyncResult()
    shop = adapter.get_shop()
    append(conn, Event(user_id=user_id, type="etsy.shop.observed", payload=shop.model_dump()))
    result.shops = 1
    for listing in adapter.list_listings():
        append(
            conn,
            Event(user_id=user_id, type="etsy.listing.observed", payload=listing.model_dump()),
        )
        result.listings += 1
        # One live call per listing (N+1) -- fine at this shop's scale
        # (~27 listings); would need batching if the catalog grew into the
        # hundreds+.
        images = adapter.get_listing_images(listing.listing_id)
        append(
            conn,
            Event(
                user_id=user_id,
                type="etsy.listing.images.observed",
                payload={
                    "listing_id": listing.listing_id,
                    "images": [
                        img.model_dump(include={"listing_image_id", "rank", "url_570xN"})
                        for img in images
                    ],
                },
            ),
        )
        result.listing_images += 1
    prior_sales = _sale_events_for_user(conn, user_id)
    last_ts = max((e.payload["created_timestamp"] for e in prior_sales), default=None)
    seen_ids = {e.payload["receipt_id"] for e in prior_sales}
    for receipt in adapter.list_receipts(min_created=last_ts):
        if receipt.receipt_id in seen_ids:
            continue
        append(
            conn,
            Event(user_id=user_id, type="etsy.sale.observed", payload=receipt.model_dump()),
        )
        result.receipts += 1
    return result
