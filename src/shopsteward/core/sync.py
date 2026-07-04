"""Pull an Etsy snapshot through the adapter and append observation events."""

import sqlite3

from pydantic import BaseModel

from shopsteward.adapters.etsy.interface import EtsyAdapter
from shopsteward.core.events import Event, append, read_all


class SyncResult(BaseModel):
    shops: int = 0
    listings: int = 0
    receipts: int = 0


def _last_receipt_ts(conn: sqlite3.Connection) -> int | None:
    sales = read_all(conn, "etsy.sale.observed")
    return max((e.payload["created_timestamp"] for e in sales), default=None)


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
    last_ts = _last_receipt_ts(conn)
    min_created = last_ts + 1 if last_ts is not None else None
    for receipt in adapter.list_receipts(min_created=min_created):
        append(
            conn,
            Event(user_id=user_id, type="etsy.sale.observed", payload=receipt.model_dump()),
        )
        result.receipts += 1
    return result
