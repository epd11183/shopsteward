"""shop build's Phase B wiring: after the digital drafts step, also build
costed physical POD drafts (variant selection + pricing + print-file
hosting, slice 1-2) through print_file_hosted -- create/link/enrich is
Phase C. Offline end to end (test_shop_build.py precedent)."""

from pathlib import Path

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.shop import LiveGateClosedError, run_shop_build

USER_ID = 1


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_MOCKUPS_DIR", str(tmp_path / "mockups"))
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _winners_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "winners"
    folder.mkdir()
    # 6000x4000, aspect 2:3 (pod.json's only configured aspect class),
    # long edge easily clears min_dpi at every acrylic/poster/canvas size.
    Image.new("RGB", (6000, 4000)).save(folder / "winner1.jpg", "JPEG")
    return folder


def test_run_shop_build_builds_pod_drafts(conn, tmp_path):
    folder = _winners_folder(tmp_path)

    result = run_shop_build(conn, USER_ID, folder)

    assert result["pod_drafts"] > 0

    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='listingdraft.print_file_hosted'"
    ).fetchone()
    assert row["n"] > 0


def test_run_shop_build_refuses_live_printfile_when_gate_closed(conn, tmp_path, monkeypatch):
    folder = _winners_folder(tmp_path)
    monkeypatch.delenv("SHOPSTEWARD_LIVE_PRINTFILE", raising=False)

    with pytest.raises(LiveGateClosedError):
        run_shop_build(conn, USER_ID, folder, live_printfile=True)

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proj_landing_files'"
    ).fetchone()
    assert row is None
