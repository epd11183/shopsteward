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
