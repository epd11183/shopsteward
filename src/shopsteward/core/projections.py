"""Derived read models, rebuilt from the event log. Safe to drop and rebuild."""

import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from shopsteward.core.events import read_all

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_listings;
CREATE TABLE proj_listings (
    user_id INTEGER NOT NULL, listing_id INTEGER NOT NULL, title TEXT NOT NULL,
    state TEXT NOT NULL, views INTEGER, num_favorers INTEGER, price_usd REAL,
    PRIMARY KEY (user_id, listing_id)
);
DROP TABLE IF EXISTS proj_sales;
CREATE TABLE proj_sales (
    user_id INTEGER NOT NULL, receipt_id INTEGER NOT NULL, sale_date TEXT NOT NULL,
    total_usd REAL NOT NULL,
    PRIMARY KEY (user_id, receipt_id)
);
"""


class ListingRow(BaseModel):
    listing_id: int
    title: str
    views: int
    num_favorers: int
    price_usd: float


class Summary(BaseModel):
    total_revenue_usd: float
    total_orders: int
    active_listings: int
    revenue_by_day: dict[str, float] = Field(default_factory=dict)
    top_listings: list[ListingRow] = Field(default_factory=list)


def rebuild(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)
    for e in read_all(conn, "etsy.listing.observed"):
        p = e.payload
        conn.execute(
            "INSERT OR REPLACE INTO proj_listings VALUES (?,?,?,?,?,?,?)",
            (
                e.user_id,
                p["listing_id"],
                p["title"],
                p["state"],
                p.get("views", 0),
                p.get("num_favorers", 0),
                p["price"]["amount"] / p["price"]["divisor"],
            ),
        )
    for e in read_all(conn, "etsy.sale.observed"):
        p = e.payload
        day = datetime.fromtimestamp(p["created_timestamp"], tz=UTC).date().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO proj_sales VALUES (?,?,?,?)",
            (
                e.user_id,
                p["receipt_id"],
                day,
                p["grandtotal"]["amount"] / p["grandtotal"]["divisor"],
            ),
        )
    conn.commit()


def analytics_summary(conn: sqlite3.Connection, user_id: int) -> Summary:
    revenue, orders = conn.execute(
        "SELECT COALESCE(SUM(total_usd),0), COUNT(*) FROM proj_sales WHERE user_id=?",
        (user_id,),
    ).fetchone()
    active = conn.execute(
        "SELECT COUNT(*) FROM proj_listings WHERE user_id=? AND state='active'", (user_id,)
    ).fetchone()[0]
    by_day = dict(
        conn.execute(
            "SELECT sale_date, SUM(total_usd) FROM proj_sales WHERE user_id=? "
            "GROUP BY sale_date ORDER BY sale_date",
            (user_id,),
        ).fetchall()
    )
    top = [
        ListingRow(
            listing_id=r["listing_id"],
            title=r["title"],
            views=r["views"],
            num_favorers=r["num_favorers"],
            price_usd=r["price_usd"],
        )
        for r in conn.execute(
            "SELECT * FROM proj_listings WHERE user_id=? ORDER BY views DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    ]
    return Summary(
        total_revenue_usd=revenue,
        total_orders=orders,
        active_listings=active,
        revenue_by_day=by_day,
        top_listings=top,
    )
