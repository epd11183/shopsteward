import sqlite3
from pathlib import Path

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "test.db")
    migrate(c)
    return c


def test_append_and_read_roundtrip(conn: sqlite3.Connection) -> None:
    e = append(conn, Event(user_id=1, type="etsy.listing.observed", payload={"listing_id": 42}))
    events = read_all(conn)
    assert [ev.id for ev in events] == [e.id]
    assert events[0].payload == {"listing_id": 42}
    assert events[0].user_id == 1


def test_events_are_immutable(conn: sqlite3.Connection) -> None:
    append(conn, Event(user_id=1, type="etsy.shop.observed", payload={}))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET type = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events")


def test_read_all_orders_by_id(conn: sqlite3.Connection) -> None:
    for i in range(3):
        append(conn, Event(user_id=1, type="t", payload={"i": i}))
    assert [e.payload["i"] for e in read_all(conn)] == [0, 1, 2]
