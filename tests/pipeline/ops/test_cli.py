from pathlib import Path

from typer.testing import CliRunner

from shopsteward.cli import app

runner = CliRunner()

FIXTURES = Path(__file__).parents[2] / "fixtures" / "etsy"


def test_ops_brief_on_a_brand_new_db_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    result = runner.invoke(app, ["ops", "brief"])
    assert result.exit_code == 0
    assert "THE SHOP" in result.output


def test_ops_brief_after_a_real_fixture_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))

    synced = runner.invoke(app, ["sync", "--fixtures", str(FIXTURES)])
    assert synced.exit_code == 0

    result = runner.invoke(app, ["ops", "brief"])
    assert result.exit_code == 0
    assert "THE SHOP" in result.output
    assert "Product mix" in result.output


def test_ops_brief_is_idempotent_and_seeds_config_only_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))

    first = runner.invoke(app, ["ops", "brief"])
    second = runner.invoke(app, ["ops", "brief"])
    assert first.exit_code == 0
    assert second.exit_code == 0

    from shopsteward.core.db import connect
    from shopsteward.core.events import read_all

    conn = connect(tmp_path / "t.db")
    seeded = [e for e in read_all(conn, "opsconfig.") if e.type == "opsconfig.seeded"]
    assert len(seeded) == 1
