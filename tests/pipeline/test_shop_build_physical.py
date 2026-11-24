"""shop build's Phase C wiring: after POD drafts reach print_file_hosted,
drive them through provider link (C1) and enrich (C2) -- all fakes by
default. --live-gelato refuses up front when its live gate is closed
(default: SHOPSTEWARD_LIVE_GELATO/GELATO_API_KEY unset), same pattern as
the other --live-* flags (pipeline/live_gate.py). Offline end to end
(test_shop_build_pod.py precedent)."""

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
    # Offline/fake POD linking must work with NO Gelato env at all (store_id
    # resolves to pod.json's harmless placeholder, which the fake ignores).
    for var in ("SHOPSTEWARD_LIVE_GELATO", "GELATO_API_KEY", "GELATO_STORE_ID"):
        monkeypatch.delenv(var, raising=False)
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


def test_pod_stages_never_get_live_etsy_or_copy_without_live_gelato(conn, tmp_path, monkeypatch):
    """--live-etsy-write/--live-copy without --live-gelato: the digital push
    keeps the live adapter, but POD link/enrich get a separate FAKE Etsy
    adapter and non-live copy (fake link mints placeholder listing ids)."""
    import shopsteward.shop as shop

    monkeypatch.setattr(shop, "live_copy_open", lambda: True)
    monkeypatch.setattr(shop, "live_etsy_write_open", lambda: True)

    etsy_calls: list[tuple[bool, object]] = []
    real_etsy = shop.build_etsy_write_adapter

    def spy_etsy(*, live: bool):
        adapter = real_etsy(live=False)  # never construct a live adapter in tests
        etsy_calls.append((live, adapter))
        return adapter

    copy_lives: list[bool] = []
    real_copy = shop.build_copy_adapter

    def spy_copy(cfg, *, live: bool):
        copy_lives.append(live)
        return real_copy(cfg, live=False)

    seen: dict[str, object] = {}
    real_link, real_enrich = shop.link_pod_drafts, shop.enrich_pod_drafts

    def spy_link(*a, **kw):
        seen["link_listings"] = getattr(kw["adapter"], "_etsy_listings", None)
        return real_link(*a, **kw)

    def spy_enrich(*a, **kw):
        seen["enrich_etsy"] = kw["etsy_adapter"]
        seen["enrich_live"] = kw["live"]
        return real_enrich(*a, **kw)

    import shopsteward.pipeline.listings.drafts as drafts_mod

    # digital build_drafts builds its own copy adapter; keep it offline
    monkeypatch.setattr(
        drafts_mod, "build_copy_adapter", lambda cfg, *, live: real_copy(cfg, live=False)
    )
    monkeypatch.setattr(shop, "build_etsy_write_adapter", spy_etsy)
    monkeypatch.setattr(shop, "build_copy_adapter", spy_copy)
    monkeypatch.setattr(shop, "link_pod_drafts", spy_link)
    monkeypatch.setattr(shop, "enrich_pod_drafts", spy_enrich)

    run_shop_build(conn, USER_ID, _winners_folder(tmp_path), live_copy=True, live_etsy_write=True)

    assert [live for live, _ in etsy_calls] == [True, False]
    digital, pod = etsy_calls[0][1], etsy_calls[1][1]
    assert seen["enrich_etsy"] is pod
    assert seen["enrich_etsy"] is not digital
    assert seen["enrich_live"] is False
    assert copy_lives == [False]
