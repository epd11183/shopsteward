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


def test_run_shop_build_twice_is_idempotent(conn, tmp_path):
    """Rerunning `shop build` on the same folder/conn must not regress an
    already-linked draft. link_pod_drafts used to gate its skip on the
    mutable pod_status ('linked'), which enrich_pod_drafts advances past
    ('enriched'/'enrich_failed') -- so a second run re-polled already-linked
    drafts against a FRESH fake adapter with no products, got a 404, and
    flipped them from 'enriched' back to 'failed'. The skip must key off the
    durable etsy_listing_id instead."""
    folder = _winners_folder(tmp_path)

    result1 = run_shop_build(conn, USER_ID, folder)
    assert result1["pod_linked"] > 0
    assert result1["pod_enriched"] > 0

    statuses_after_1 = {
        r["draft_id"]: r["pod_status"]
        for r in conn.execute("SELECT draft_id, pod_status FROM proj_listing_drafts").fetchall()
    }
    enriched_after_1 = {d for d, s in statuses_after_1.items() if s == "enriched"}
    assert enriched_after_1

    result2 = run_shop_build(conn, USER_ID, folder)

    # Nothing already linked gets re-created; only a draft that never got a
    # provider_product_id in run 1 (e.g. a catalog gap) legitimately retries.
    assert result2["pod_link_created"] == 0

    statuses_after_2 = {
        r["draft_id"]: r["pod_status"]
        for r in conn.execute("SELECT draft_id, pod_status FROM proj_listing_drafts").fetchall()
    }
    for draft_id in enriched_after_1:
        assert statuses_after_2[draft_id] != "failed"


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
