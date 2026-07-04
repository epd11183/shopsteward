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


def _make_jpeg(path: Path, size=(50, 50)) -> None:
    from PIL import Image

    Image.new("RGB", size, (120, 130, 140)).save(path, "JPEG")


def test_pipeline_score_run_live_vision_forbidden_without_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "pipeline.db"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_VISION", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    client = TestClient(create_app())
    resp = client.post("/api/pipeline/score/run", json={"live_vision": True})
    assert resp.status_code == 403
    assert "PRD" in resp.json()["detail"]


class _AlwaysPassVision:
    """Deterministic stand-in for FixtureVisionAdapter: always clears the
    Gate 1 threshold, regardless of the (pseudo-random, hash-derived) fixture
    score the tiny test JPEG would otherwise produce."""

    def score_commercial(self, jpeg_bytes: bytes, *, model: str):
        from shopsteward.adapters.vision.interface import VisionResult, VisionVerdict

        return VisionResult(
            verdict=VisionVerdict(
                commercial_score=90,
                subject="lake house",
                strongest_room_style="coastal",
                one_risk="none flagged",
                rationale="stubbed pass",
            )
        )


def test_pipeline_gate1_queue_decide_preview_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "pipeline.db"))
    monkeypatch.setenv("SHOPSTEWARD_BRIDGE_DIR", str(tmp_path / "bridge"))
    monkeypatch.setattr("shopsteward.pipeline.api.FixtureVisionAdapter", _AlwaysPassVision)

    shoot = tmp_path / "shoot"
    shoot.mkdir()
    (shoot / "hero1.CR3").write_bytes(b"raw-hero1")
    _make_jpeg(shoot / "hero1.jpg")

    client = TestClient(create_app())
    ingest_resp = client.post("/api/editing/ingest", json={"path": str(shoot), "mode": "hero"})
    assert ingest_resp.status_code == 200, ingest_resp.text

    score_resp = client.post("/api/pipeline/score/run", json={})
    assert score_resp.status_code == 200, score_resp.text

    queue_resp = client.get("/api/pipeline/gate1/queue", params={"state": "pending"})
    assert queue_resp.status_code == 200
    queue = queue_resp.json()
    assert len(queue) == 1

    photo_id = queue[0]["photo_id"]

    preview_resp = client.get(f"/api/pipeline/gate1/photo/{photo_id}/preview")
    assert preview_resp.status_code == 200

    decide_resp = client.post(
        "/api/pipeline/gate1/decide", json={"photo_id": photo_id, "decision": "approve"}
    )
    assert decide_resp.status_code == 200, decide_resp.text
    card = decide_resp.json()
    assert card["state"] == "approved"
    assert card["edit_job_id"] is not None

    # Double-approve is invalid: the photo is no longer pending.
    redecide_resp = client.post(
        "/api/pipeline/gate1/decide", json={"photo_id": photo_id, "decision": "approve"}
    )
    assert redecide_resp.status_code == 400

    undo_resp = client.post("/api/pipeline/gate1/undo", json={"photo_id": photo_id})
    assert undo_resp.status_code == 200
    assert undo_resp.json()["undo_of"] == "approved"


def test_pipeline_gate1_preview_unknown_photo_404(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "pipeline.db"))
    client = TestClient(create_app())
    resp = client.get("/api/pipeline/gate1/photo/does-not-exist/preview")
    assert resp.status_code == 404


def test_pipeline_gate1_decide_unknown_photo_400(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "pipeline.db"))
    monkeypatch.setenv("SHOPSTEWARD_BRIDGE_DIR", str(tmp_path / "bridge"))
    client = TestClient(create_app())
    resp = client.post(
        "/api/pipeline/gate1/decide", json={"photo_id": "does-not-exist", "decision": "approve"}
    )
    assert resp.status_code == 400


def test_pipeline_landing_scan_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "pipeline.db"))
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    monkeypatch.setenv("SHOPSTEWARD_LANDING_DIR", str(landing_dir))

    from PIL import Image

    Image.new("RGB", (3200, 3200)).save(landing_dir / "unrelated_shot.jpg", "JPEG")

    client = TestClient(create_app())
    resp = client.post("/api/pipeline/landing/scan", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["observed"] == 1
    assert body["matched"] == 0
    assert body["manual_drops"] == 1
    assert body["invalid"] == 0
