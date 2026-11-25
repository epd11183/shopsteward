from typer.testing import CliRunner

from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append


def _db(tmp_path, monkeypatch, *, synced: bool):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    conn = connect(db)
    migrate(conn)
    if synced:
        append(conn, Event(user_id=1, type="etsy.shop.observed", payload={}))
    conn.close()


def test_heartbeat_stale_when_never_synced(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, synced=False)
    result = CliRunner().invoke(app, ["ops", "heartbeat"])
    assert result.exit_code == 1 and "STALE" in result.output


def test_heartbeat_ok_after_a_fresh_sync_and_stale_past_max_age(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, synced=True)
    ok = CliRunner().invoke(app, ["ops", "heartbeat"])
    assert ok.exit_code == 0 and ok.output.startswith("ok:")
    stale = CliRunner().invoke(app, ["ops", "heartbeat", "--max-age-days", "0"])
    assert stale.exit_code == 1 and "STALE" in stale.output


def _insert(db, event_type: str, created_at: str) -> None:
    conn = connect(db)
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (1, ?, '{}', ?)",
        (event_type, created_at),
    )
    conn.commit()
    conn.close()


def test_heartbeat_goes_stale_when_only_syncs_happen_after_the_last_run(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, synced=False)
    db = tmp_path / "t.db"
    _insert(db, "ops.run.completed", "2020-01-01T00:00:00.000Z")
    append_conn = connect(db)
    append(append_conn, Event(user_id=1, type="etsy.shop.observed", payload={}))  # fresh daily sync
    append_conn.close()
    result = CliRunner().invoke(app, ["ops", "heartbeat"])
    assert result.exit_code == 1 and "last live ops run" in result.output


def test_heartbeat_ok_after_a_fresh_ops_run_even_if_the_sync_is_old(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, synced=False)
    db = tmp_path / "t.db"
    _insert(db, "etsy.shop.observed", "2020-01-01T00:00:00.000Z")
    c = connect(db)
    append(c, Event(user_id=1, type="ops.run.completed", payload={}))
    c.close()
    result = CliRunner().invoke(app, ["ops", "heartbeat"])
    assert result.exit_code == 0 and result.output.startswith("ok:")


def test_heartbeat_falls_back_to_the_sync_until_a_run_event_exists(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, synced=True)  # only etsy.shop.observed, fresh
    result = CliRunner().invoke(app, ["ops", "heartbeat"])
    assert result.exit_code == 0
