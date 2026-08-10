import pytest

from shopsteward.adapters.vision.fake import FakeVisionAdapter
from shopsteward.adapters.vision.interface import VisionResult, VisionUsage, VisionVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.vision_copy import VisionCostCapError, run_vision_copy
from shopsteward.pipeline.projections import rebuild_pipeline

USER = 1


def _landing_winner(conn, file_id="abc123def456", path="/w/A.jpg"):
    append(conn, Event(user_id=USER, type="landing.file_observed", payload={
        "file_id": file_id, "path": path, "base_name": "A", "photo_id": None,
        "format": "JPEG", "width": 4000, "height": 3000, "color_space": "sRGB"}))
    rebuild_pipeline(conn)


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def _verdict_result():
    return VisionResult(
        verdict=VisionVerdict(
            commercial_score=80, subject="trail runner", strongest_room_style="modern",
            one_risk="busy background", rationale="dynamic motion"),
        usage=VisionUsage(model="m", est_cost_usd=0.01))


def test_scores_photoless_winner_under_synthetic_id(monkeypatch):
    c = _conn()
    _landing_winner(c)
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    out = run_vision_copy(c, USER, adapter=FakeVisionAdapter([_verdict_result()]),
                          model="m", soft_cap_usd=5.0, month_prefix="2026-08")
    assert out["scored"] == 1
    rebuild_pipeline(c)
    row = c.execute("SELECT subject FROM proj_scores WHERE user_id=? AND photo_id=?",
                    (USER, "file-abc123def456")).fetchone()
    assert row["subject"] == "trail runner"


def test_idempotent_skip(monkeypatch):
    c = _conn()
    _landing_winner(c)
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    run_vision_copy(c, USER, adapter=FakeVisionAdapter([_verdict_result()]),
                    model="m", soft_cap_usd=5.0, month_prefix="2026-08")
    out = run_vision_copy(c, USER, adapter=FakeVisionAdapter([]),
                          model="m", soft_cap_usd=5.0, month_prefix="2026-08")
    assert out["scored"] == 0 and out["skipped"] == 1


def test_soft_cap_refuses(monkeypatch):
    c = _conn()
    _landing_winner(c)
    append(c, Event(user_id=USER, type="llm.call",
                    payload={"feature": "vision_copy", "est_cost_usd": 5.0}))
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    with pytest.raises(VisionCostCapError):
        run_vision_copy(c, USER, adapter=FakeVisionAdapter([_verdict_result()]),
                        model="m", soft_cap_usd=5.0, month_prefix="2026-08")
