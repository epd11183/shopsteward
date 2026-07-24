"""Shared llm.call monthly-spend ledger (PRD §13 decisions 22, 38): the
$10/mo vision soft cap (M3) now also gates listing copy generation (M5a).
One function so scoring.py and listings/copy.py read the same ledger."""

import sqlite3
from datetime import UTC, datetime

from shopsteward.core.events import read_all


def current_month_prefix() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def monthly_spend(conn: sqlite3.Connection, user_id: int, month_prefix: str | None = None) -> float:
    month_prefix = month_prefix or current_month_prefix()
    total = 0.0
    for e in read_all(conn, "llm.call"):
        if e.user_id != user_id or not (e.created_at or "").startswith(month_prefix):
            continue
        cost = e.payload.get("est_cost_usd")
        if cost is not None:
            total += cost
    return total
