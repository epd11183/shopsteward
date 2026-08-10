"""shop build's Phase C wiring: after POD drafts reach print_file_hosted,
drive them through provider link (C1) and enrich (C2) -- all fakes by
default. --live-gelato refuses up front since the live Gelato adapter
(C3) isn't built yet (pod/factory.py::build_pod_adapter). Offline end to
end (test_shop_build_pod.py precedent)."""

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


def test_run_shop_build_links_and_enriches_pod_drafts(conn, tmp_path):
    folder = _winners_folder(tmp_path)

    result = run_shop_build(conn, USER_ID, folder)

    assert result["pod_linked"] > 0
    assert result["pod_enriched"] > 0

    linked = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='listingdraft.provider_linked'"
    ).fetchone()
    assert linked["n"] > 0

    enriched = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='listingdraft.enriched'"
    ).fetchone()
    assert enriched["n"] > 0


def test_run_shop_build_refuses_live_gelato_before_creating_products(conn, tmp_path):
    folder = _winners_folder(tmp_path)

    with pytest.raises(LiveGateClosedError):
        run_shop_build(conn, USER_ID, folder, live_gelato=True)

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proj_landing_files'"
    ).fetchone()
    assert row is None

    created = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='listingdraft.provider_created'"
    ).fetchone()
    assert created["n"] == 0
