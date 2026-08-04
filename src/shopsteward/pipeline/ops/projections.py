"""Derived read models for M8a slice 1, rebuilt from the event log. Safe to
drop and rebuild (rebuild_ops()) -- editing/mockups/listings precedent: the
projection table lives in projections.py, not config.py.

proj_listing_daily is the whole point of this slice (design §0(a)/§7/§9):
core/projections.py's rebuild() folds etsy.listing.observed into
proj_listings with INSERT OR REPLACE keyed on listing_id alone, so one
day's sync overwrites the last -- the per-listing history is discarded.
Here the fold buckets on the EVENT's own created_at date and keys on
(user_id, listing_id, day), so N observations of one listing across N
distinct days produce N rows, and "last observation within a day wins"
only collapses same-day re-syncs.

proj_sale_items is the one new sales projection this slice adds. Design
§0(a)/§8.3 asks to reuse an existing sales/receipts projection rather than
duplicate one: core/projections.py's proj_sales already exists and is
reused as-is (rebuild_core() must be called separately -- see
pipeline/ops/cli.py) for receipt-level revenue-by-day. It has no
listing_id, though, so it cannot answer "which listings sold" or "revenue
by product type" -- the analytics this slice exists to produce. That
attribution is genuinely new, so proj_sale_items folds
etsy.sale.observed's transactions[] (each already carries listing_id,
per §0(a)) into one row per transaction. It does NOT re-derive receipt
totals -- analytics.py sums proj_sale_items only for listing-level
breakdowns and reads core's proj_sales for shop-wide revenue, so the two
are never a source of disagreement about total revenue.

proj_ops_config mirrors pod/projections.py's proj_pod_config exactly.

This module owns no capability/action/ladder projections -- those are the
autonomy chassis (design §8.1/§8.3) and belong to slice 2+."""

import json
import sqlite3
from datetime import UTC, datetime

from shopsteward.core.events import read_all
from shopsteward.pipeline.ops.config import OPS_CONFIG_EVENT_TYPES

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_listing_daily;
CREATE TABLE proj_listing_daily (
    user_id INTEGER NOT NULL, listing_id INTEGER NOT NULL, day TEXT NOT NULL,
    views INTEGER NOT NULL, num_favorers INTEGER NOT NULL, price_usd REAL NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (user_id, listing_id, day)
);
DROP TABLE IF EXISTS proj_sale_items;
CREATE TABLE proj_sale_items (
    user_id INTEGER NOT NULL, transaction_id INTEGER NOT NULL, receipt_id INTEGER NOT NULL,
    listing_id INTEGER NOT NULL, quantity INTEGER NOT NULL, price_usd REAL NOT NULL,
    sale_date TEXT NOT NULL,
    PRIMARY KEY (user_id, transaction_id)
);
DROP TABLE IF EXISTS proj_ops_config;
CREATE TABLE proj_ops_config (
    user_id INTEGER NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
"""


def rebuild_ops(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)

    for e in read_all(conn, "etsy.listing.observed"):
        if e.created_at is None:
            continue  # defensive -- the events table default always sets this
        day = e.created_at[:10]
        p = e.payload
        conn.execute(
            "INSERT OR REPLACE INTO proj_listing_daily VALUES (?,?,?,?,?,?,?)",
            (
                e.user_id,
                p["listing_id"],
                day,
                p.get("views", 0),
                p.get("num_favorers", 0),
                p["price"]["amount"] / p["price"]["divisor"],
                p["state"],
            ),
        )

    for e in read_all(conn, "etsy.sale.observed"):
        p = e.payload
        sale_date = datetime.fromtimestamp(p["created_timestamp"], tz=UTC).date().isoformat()
        for t in p.get("transactions", []):
            conn.execute(
                "INSERT OR REPLACE INTO proj_sale_items VALUES (?,?,?,?,?,?,?)",
                (
                    e.user_id,
                    t["transaction_id"],
                    p["receipt_id"],
                    t["listing_id"],
                    t["quantity"],
                    t["price"]["amount"] / t["price"]["divisor"],
                    sale_date,
                ),
            )

    for e in read_all(conn, "opsconfig."):
        # Guard against a future event in the same namespace this fold
        # doesn't know how to handle yet (pod/projections.py precedent).
        if e.type not in OPS_CONFIG_EVENT_TYPES:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO proj_ops_config VALUES (?,?,?)",
            (e.user_id, e.payload["name"], json.dumps(e.payload["config"])),
        )

    conn.commit()
