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
    assert body["total_revenue_usd"] == 250.0
    assert body["active_listings"] == 7
    assert len(body["top_listings"]) == 7


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
    monkeypatch.setattr(
        "shopsteward.pipeline.vision_factory.FixtureVisionAdapter", _AlwaysPassVision
    )

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


def test_mockups_templates_scan_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "mockups.db"))
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))

    client = TestClient(create_app())
    scan_resp = client.post("/api/pipeline/templates/scan", json={})
    assert scan_resp.status_code == 200
    registered = scan_resp.json()["registered"]
    assert registered >= 4

    list_resp = client.get("/api/pipeline/templates")
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert len(rows) == registered
    assert all(row["status"] == "valid" for row in rows)


def test_mockups_templates_annotate_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "mockups.db"))
    operator_dir = tmp_path / "operator_templates"
    operator_dir.mkdir()
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(operator_dir))

    image_path = operator_dir / "annotated-01.jpg"
    _make_jpeg(image_path, size=(800, 600))

    client = TestClient(create_app())
    resp = client.post(
        "/api/pipeline/templates/annotate",
        json={
            "image_path": str(image_path),
            "sidecar": {
                "schema": "shopsteward.stagingtemplate/1",
                "template_id": "annotated-01",
                "room_type": "living_room",
                "style": "modern",
                "lighting": "warm_daylight",
                "orientation": "landscape",
                "regions": [
                    {
                        "kind": "wall_print",
                        "quad": [[100.0, 100.0], [700.0, 110.0], [690.0, 500.0], [110.0, 495.0]],
                        "region_width_inches": 30.0,
                    }
                ],
                "tags": ["neutral_wall"],
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["invalid_reason"] is None
    assert body["template"]["template_id"] == "annotated-01"
    assert body["template"]["status"] == "valid"
    assert (operator_dir / "annotated-01.template.json").is_file()


def test_mockups_run_and_list_and_image_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "mockups.db"))
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    mockups_dir = tmp_path / "mockups_out"
    monkeypatch.setenv("SHOPSTEWARD_MOCKUPS_DIR", str(mockups_dir))

    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    from PIL import Image

    Image.new("RGB", (3600, 2400), (150, 130, 110)).save(landing_dir / "hero.tif", format="TIFF")
    monkeypatch.setenv("SHOPSTEWARD_LANDING_DIR", str(landing_dir))

    client = TestClient(create_app())
    scan_resp = client.post("/api/pipeline/landing/scan", json={})
    assert scan_resp.status_code == 200
    assert scan_resp.json()["observed"] == 1

    run_resp = client.post("/api/pipeline/mockups/run", json={})
    assert run_resp.status_code == 200, run_resp.text
    result = run_resp.json()
    assert result["sets_completed"] == 1
    assert result["mockups_written"] > 0

    list_resp = client.get("/api/pipeline/mockups")
    assert list_resp.status_code == 200
    records = list_resp.json()
    assert len(records) == result["mockups_written"]

    good_path = records[0]["path"]
    image_resp = client.get("/api/pipeline/mockups/image", params={"path": good_path})
    assert image_resp.status_code == 200

    traversal_path = str(Path(good_path).parent / ".." / ".." / ".." / "etc" / "passwd")
    escape_resp = client.get("/api/pipeline/mockups/image", params={"path": traversal_path})
    assert escape_resp.status_code == 403

    outside_resp = client.get("/api/pipeline/templates/image", params={"path": good_path})
    assert outside_resp.status_code == 403


# --- M5a slice 4: listings build + Gate 3 ----------------------------------


def _build_one_pushed_draft_via_api(client, tmp_path, monkeypatch):
    """landing/scan -> mockups/run -> listings/build, all through the API,
    mirroring the real operator flow. Returns the pushed draft_id."""
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    mockups_dir = tmp_path / "mockups_out"
    monkeypatch.setenv("SHOPSTEWARD_MOCKUPS_DIR", str(mockups_dir))

    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    _make_jpeg(landing_dir / "hero.jpg", size=(3600, 2400))
    monkeypatch.setenv("SHOPSTEWARD_LANDING_DIR", str(landing_dir))

    assert client.post("/api/pipeline/landing/scan", json={}).status_code == 200
    mockups_resp = client.post("/api/pipeline/mockups/run", json={})
    assert mockups_resp.status_code == 200, mockups_resp.text
    assert mockups_resp.json()["mockups_written"] > 0

    build_resp = client.post("/api/pipeline/listings/build", json={})
    assert build_resp.status_code == 200, build_resp.text
    report = build_resp.json()
    assert report["pushed"] == 1

    queue = client.get("/api/pipeline/gate3/queue").json()
    assert len(queue) == 1
    return queue[0]["draft_id"]


def test_gate3_queue_edit_publish_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "listings.db"))
    client = TestClient(create_app())
    draft_id = _build_one_pushed_draft_via_api(client, tmp_path, monkeypatch)

    queue = client.get("/api/pipeline/gate3/queue").json()
    card = queue[0]
    assert card["state"] == "pushed"
    assert card["economics"]["price"] == card["price"]
    assert len(card["images"]) >= 1

    image_resp = client.get(
        f"/api/pipeline/gate3/draft/{draft_id}/image", params={"path": card["images"][0]["path"]}
    )
    assert image_resp.status_code == 200

    below_floor_resp = client.post(
        "/api/pipeline/gate3/edit", json={"draft_id": draft_id, "price": 1.00}
    )
    assert below_floor_resp.status_code == 400

    edit_resp = client.post(
        "/api/pipeline/gate3/edit", json={"draft_id": draft_id, "title": "Custom Title"}
    )
    assert edit_resp.status_code == 200, edit_resp.text
    assert edit_resp.json()["title"] == "Custom Title"

    publish_resp = client.post("/api/pipeline/gate3/publish", json={"draft_id": draft_id})
    assert publish_resp.status_code == 200, publish_resp.text
    assert publish_resp.json()["state"] == "published"

    # published drafts drop out of the queue
    assert client.get("/api/pipeline/gate3/queue").json() == []

    # publishing again is rejected (no longer publishable)
    republish_resp = client.post("/api/pipeline/gate3/publish", json={"draft_id": draft_id})
    assert republish_resp.status_code == 400


def test_gate3_edit_unknown_draft_400(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "listings.db"))
    client = TestClient(create_app())
    resp = client.post("/api/pipeline/gate3/edit", json={"draft_id": "nope", "title": "x"})
    assert resp.status_code == 400


def test_gate3_image_traversal_403(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "listings.db"))
    client = TestClient(create_app())
    resp = client.get("/api/pipeline/gate3/draft/some-draft/image", params={"path": "/etc/passwd"})
    assert resp.status_code == 403


def test_gate3_retry_after_push_failure(tmp_path, monkeypatch):
    """Uses the API's own shared Fake adapter cache (keyed by db_path) to
    inject one create_draft_listing failure, then exercises POST
    /gate3/retry against the resulting push_failed draft."""
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "listings.db"))
    from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
    from shopsteward.adapters.etsy.interface import EtsyWriteError
    from shopsteward.pipeline.listings import api as listings_api
    from shopsteward.settings import db_path

    class _FailOnceAdapter(FakeEtsyWriteAdapter):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def create_draft_listing(self, spec):
            if self.fail_next:
                self.fail_next = False
                raise EtsyWriteError(500, "simulated failure")
            return super().create_draft_listing(spec)

    listings_api._fake_adapters[str(db_path())] = _FailOnceAdapter()

    client = TestClient(create_app())
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    monkeypatch.setenv("SHOPSTEWARD_MOCKUPS_DIR", str(tmp_path / "mockups_out"))
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    _make_jpeg(landing_dir / "hero.jpg", size=(3600, 2400))
    monkeypatch.setenv("SHOPSTEWARD_LANDING_DIR", str(landing_dir))

    client.post("/api/pipeline/landing/scan", json={})
    client.post("/api/pipeline/mockups/run", json={})
    build_resp = client.post("/api/pipeline/listings/build", json={})
    assert build_resp.json()["push_failed"] == 1

    queue = client.get("/api/pipeline/gate3/queue").json()
    assert len(queue) == 1
    draft_id = queue[0]["draft_id"]
    assert queue[0]["state"] == "push_failed"

    retry_resp = client.post("/api/pipeline/gate3/retry", json={"draft_id": draft_id})
    assert retry_resp.status_code == 200, retry_resp.text
    assert retry_resp.json()["state"] == "pushed"

    no_retry_resp = client.post("/api/pipeline/gate3/retry", json={"draft_id": draft_id})
    assert no_retry_resp.status_code == 400
