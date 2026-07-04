from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.api import create_app
from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild
from shopsteward.core.sync import sync_etsy

FIXTURES = Path(__file__).parent / "fixtures" / "etsy"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    conn = connect(db)
    migrate(conn)
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    rebuild(conn)
    conn.close()
    return TestClient(create_app())


def test_analytics_summary_endpoint(client):
    body = client.get("/api/analytics/summary").json()
    assert body["total_revenue_usd"] == 72.0
    assert body["active_listings"] == 3
    assert len(body["top_listings"]) == 3


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_summary_on_fresh_db_returns_zeros(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "fresh.db"))
    resp = TestClient(create_app()).get("/api/analytics/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_revenue_usd"] == 0
    assert body["total_orders"] == 0
    assert body["active_listings"] == 0


def test_editing_preset_families_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "editing.db"))
    resp = TestClient(create_app()).get("/api/editing/preset-families")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 4
    assert {f["name"] for f in body} == {"neutral", "wedding", "race", "brewery"}


def test_editing_jobs_endpoint_empty_on_fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "editing.db"))
    resp = TestClient(create_app()).get("/api/editing/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ingest_jobs": [], "edit_jobs": [], "photos": {}}


def test_editing_ingest_mass_invalid_preset_rejected_before_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "editing.db"))
    monkeypatch.setenv("SHOPSTEWARD_BRIDGE_DIR", str(tmp_path / "bridge"))
    shoot = tmp_path / "shoot"
    shoot.mkdir()

    client = TestClient(create_app())
    resp = client.post(
        "/api/editing/ingest",
        json={"path": str(shoot), "mode": "mass", "preset_family": "nope"},
    )
    assert resp.status_code == 400
    assert "neutral" in resp.json()["detail"]  # lists available families

    # Rejected before ingest: no ingest job was recorded.
    jobs = client.get("/api/editing/jobs").json()
    assert jobs["ingest_jobs"] == []


def test_editing_ingest_mass_missing_preset_rejected_before_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "editing.db"))
    monkeypatch.setenv("SHOPSTEWARD_BRIDGE_DIR", str(tmp_path / "bridge"))
    shoot = tmp_path / "shoot"
    shoot.mkdir()

    client = TestClient(create_app())
    resp = client.post("/api/editing/ingest", json={"path": str(shoot), "mode": "mass"})
    assert resp.status_code == 400

    jobs = client.get("/api/editing/jobs").json()
    assert jobs["ingest_jobs"] == []


def test_editing_ingest_mass_empty_folder_skips_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "editing.db"))
    monkeypatch.setenv("SHOPSTEWARD_BRIDGE_DIR", str(tmp_path / "bridge"))
    shoot = tmp_path / "shoot"
    shoot.mkdir()

    client = TestClient(create_app())
    resp = client.post(
        "/api/editing/ingest",
        json={"path": str(shoot), "mode": "mass", "preset_family": "neutral"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["edit_job_id"] is None
    assert body["report"]["paired"] == 0
    # No empty edit job was dispatched to the bridge.
    assert not (tmp_path / "bridge" / "jobs").exists()
