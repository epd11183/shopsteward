"""Pull an Etsy snapshot through the adapter and append observation events."""

import sqlite3

from pydantic import BaseModel

from shopsteward.adapters.etsy.interface import EtsyAdapter
from shopsteward.core.events import Event, append, read_all


class SyncResult(BaseModel):
    shops: int = 0
    listings: int = 0
    receipts: int = 0


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
