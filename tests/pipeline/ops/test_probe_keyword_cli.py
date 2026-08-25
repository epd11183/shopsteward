"""`ops probe-keyword` -- fixture-gated CLI verb, --fixtures/--live-etsy-read
mutual exclusion (adopt-local precedent), and the event it appends."""

from pathlib import Path

from typer.testing import CliRunner

from shopsteward.cli import app
from shopsteward.core.db import connect
from shopsteward.core.events import read_all

runner = CliRunner()

FIXTURES = Path(__file__).parents[2] / "fixtures" / "etsy"


def test_probe_keyword_requires_fixtures_or_live_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    result = runner.invoke(app, ["ops", "probe-keyword", "sandhill crane print"])
    assert result.exit_code == 1
    assert "pass --fixtures or --live-etsy-read" in result.output


def test_probe_keyword_rejects_both_fixtures_and_live_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    result = runner.invoke(
        app,
        [
            "ops",
            "probe-keyword",
            "sandhill crane print",
            "--fixtures",
            str(FIXTURES),
            "--live-etsy-read",
        ],
    )
    assert result.exit_code == 1
    assert "not both" in result.output


def test_probe_keyword_against_fixtures_prints_readout_and_appends_event(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    result = runner.invoke(
        app, ["ops", "probe-keyword", "sandhill crane print", "--fixtures", str(FIXTURES)]
    )
    assert result.exit_code == 0, result.output
    assert "competition (matching listings): 5" in result.output

    conn = connect(db)
    events = read_all(conn, "etsy.keyword.probed")
    assert len(events) == 1
    assert events[0].payload["phrase"] == "sandhill crane print"


def test_probe_keyword_multiple_phrases_capped_by_max_phrases_per_run(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    # keyword_probe.max_phrases_per_run defaults to 5 -- 6 phrases must warn
    # and only probe the first 5.
    phrases = [f"phrase {i}" for i in range(6)]
    result = runner.invoke(app, ["ops", "probe-keyword", *phrases, "--fixtures", str(FIXTURES)])
    assert result.exit_code == 0, result.output
    assert "only probing the first 5 of 6 phrases" in result.output

    conn = connect(db)
    events = read_all(conn, "etsy.keyword.probed")
    assert len(events) == 5
