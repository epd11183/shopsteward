"""Append-only event log. Projections rebuild derived state from here."""

import json
import sqlite3

from pydantic import BaseModel


class Event(BaseModel):
    id: int | None = None
    user_id: int
    type: str
    payload: dict
    created_at: str | None = None


def append(conn: sqlite3.Connection, event: Event) -> Event:
    cur = conn.execute(
        "INSERT INTO events (user_id, type, payload) VALUES (?, ?, ?)",
        (event.user_id, event.type, json.dumps(event.payload)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _from_row(row)


def read_all(conn: sqlite3.Connection, type_prefix: str | None = None) -> list[Event]:
    if type_prefix:
        rows = conn.execute(
            "SELECT * FROM events WHERE type LIKE ? ORDER BY id", (type_prefix + "%",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    return [_from_row(r) for r in rows]


def _from_row(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        user_id=row["user_id"],
        type=row["type"],
        payload=json.loads(row["payload"]),
        created_at=row["created_at"],
    )
