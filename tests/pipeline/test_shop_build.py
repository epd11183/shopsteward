"""One-command shop build: scan_landing -> vision-for-copy (fixture) ->
mockups compositing -> build_drafts, offline end to end."""

from pathlib import Path

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.shop import LiveGateClosedError, run_shop_build

USER_ID = 1


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Isolate mockups output + the operator templates dir so this test only
    # ever touches tmp_path and the 4 committed placeholder templates under
    # config/defaults/staging_templates (tests/mockups/test_jobs.py precedent).
    monkeypatch.setenv("SHOPSTEWARD_MOCKUPS_DIR", str(tmp_path / "mockups"))
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _winners_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "winners"
    folder.mkdir()
    Image.new("RGB", (3200, 3200)).save(folder / "winner1.jpg", "JPEG")
    return folder


def test_offline_shop_build_skips_pod_stages_and_writes_no_pod_events(conn, tmp_path):
    result = run_shop_build(conn, USER_ID, _winners_folder(tmp_path))

    assert result["pod_stages"] == "POD stages skipped: --live-gelato not set"
    assert result["pod_drafts"] == result["pod_linked"] == result["pod_enriched"] == 0
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type IN ("
        "'listingdraft.provider_created','listingdraft.provider_linked',"
        "'listingdraft.print_file_hosted','listingdraft.pod_skipped',"
        "'listingdraft.enrich_failed','listingdraft.enriched')"
    ).fetchone()["n"]
    assert n == 0


def test_run_shop_build_end_to_end(conn, tmp_path):
    folder = _winners_folder(tmp_path)

    result = run_shop_build(conn, USER_ID, folder)

    assert result["observed"] >= 1
    assert result["scored"] >= 1
    assert result["mockup_sets"] >= 1
    assert result["drafts"] >= 1

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
    ).fetchone()
    assert row["n"] >= 1


def test_run_shop_build_refuses_live_vision_when_gate_closed(conn, tmp_path, monkeypatch):
    folder = _winners_folder(tmp_path)
    monkeypatch.delenv("SHOPSTEWARD_LIVE_VISION", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(LiveGateClosedError):
        run_shop_build(conn, USER_ID, folder, live_vision=True)

    # Refused up front, before scan_landing even runs -- no projection
    # tables for this run get created at all.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proj_landing_files'"
    ).fetchone()
    assert row is None
