from pathlib import Path

from typer.testing import CliRunner

from shopsteward.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parents[2] / "fixtures" / "etsy"


def test_adopt_local_requires_fixtures_or_live(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    result = runner.invoke(app, ["archive", "adopt-local", str(local_dir)])
    assert result.exit_code == 1
    assert "--fixtures or --live-etsy-read" in result.output


def test_adopt_local_rejects_both_fixtures_and_live(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    result = runner.invoke(
        app,
        ["archive", "adopt-local", str(local_dir), "--fixtures", str(FIXTURES), "--live-etsy-read"],
    )
    assert result.exit_code == 1
    assert "not both" in result.output


def test_adopt_local_live_without_env_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_READ", raising=False)
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    result = runner.invoke(app, ["archive", "adopt-local", str(local_dir), "--live-etsy-read"])
    assert result.exit_code == 1
    assert "gated on operator approval" in result.output


def test_adopt_local_dry_run_prints_table_and_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    local_dir = tmp_path / "local"
    local_dir.mkdir()

    result = runner.invoke(
        app, ["archive", "adopt-local", str(local_dir), "--fixtures", str(FIXTURES)]
    )
    assert result.exit_code == 0, result.output
    assert "listing_id" in result.output
    assert "verdict" in result.output
    assert "Dry-run: nothing written" in result.output

    from shopsteward.core.db import connect
    from shopsteward.core.events import read_all

    conn = connect(db)
    assert not read_all(conn, "listing.source_adopted")
    assert not read_all(conn, "asset.archived")
