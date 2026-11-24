from datetime import UTC, datetime

from shopsteward.adapters.vision.fake import FakeVisionAdapter
from shopsteward.adapters.vision.interface import VisionResult, VisionUsage, VisionVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.vision_copy import run_vision_copy
from shopsteward.pipeline.projections import rebuild_pipeline

USER = 1
# Current UTC month -- the planted llm.call is stamped now by the DB, so the
# soft-cap test must query the same month or the window goes empty once the
# month rolls over (the rest of this file plants no spend, so their fixed
# month_prefix is harmless and left as-is).
_MONTH = datetime.now(UTC).strftime("%Y-%m")


def _landing_winner(conn, file_id="abc123def456", path="/w/A.jpg", photo_id=None):
    append(
        conn,
        Event(
            user_id=USER,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": path,
                "base_name": "A",
                "photo_id": photo_id,
                "format": "JPEG",
                "width": 4000,
                "height": 3000,
                "color_space": "sRGB",
            },
        ),
    )
    rebuild_pipeline(conn)


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def _verdict_result():
    return VisionResult(
        verdict=VisionVerdict(
            commercial_score=80,
            subject="trail runner",
            strongest_room_style="modern",
            one_risk="busy background",
            rationale="dynamic motion",
        ),
        usage=VisionUsage(model="m", est_cost_usd=0.01),
    )


def test_scores_photoless_winner_under_synthetic_id(monkeypatch):
    c = _conn()
    _landing_winner(c)
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    out = run_vision_copy(
        c,
        USER,
        adapter=FakeVisionAdapter([_verdict_result()]),
        model="m",
        soft_cap_usd=5.0,
        month_prefix="2026-08",
    )
    assert out["scored"] == 1
    rebuild_pipeline(c)
    row = c.execute(
        "SELECT subject FROM proj_scores WHERE user_id=? AND photo_id=?",
        (USER, "file-abc123def456"),
    ).fetchone()
    assert row["subject"] == "trail runner"


def test_idempotent_skip(monkeypatch):
    c = _conn()
    _landing_winner(c)
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    run_vision_copy(
        c,
        USER,
        adapter=FakeVisionAdapter([_verdict_result()]),
        model="m",
        soft_cap_usd=5.0,
        month_prefix="2026-08",
    )
    out = run_vision_copy(
        c, USER, adapter=FakeVisionAdapter([]), model="m", soft_cap_usd=5.0, month_prefix="2026-08"
    )
    assert out["scored"] == 0 and out["skipped"] == 1


def test_soft_cap_stops_scoring_without_raising(monkeypatch):
    c = _conn()
    _landing_winner(c)
    append(
        c,
        Event(
            user_id=USER, type="llm.call", payload={"feature": "vision_copy", "est_cost_usd": 5.0}
        ),
    )
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    out = run_vision_copy(
        c,
        USER,
        adapter=FakeVisionAdapter([_verdict_result()]),
        model="m",
        soft_cap_usd=5.0,
        month_prefix=_MONTH,
    )
    assert out["scored"] == 0 and out["cap_hit"] is True


def test_scores_catalog_matched_winner_under_its_photo_id(monkeypatch):
    """A file matched to a catalog photo used to be skipped, so copy saw
    subject "unspecified" and wrote a generic landscape."""
    c = _conn()
    _landing_winner(c, photo_id="photo-42")
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    out = run_vision_copy(
        c,
        USER,
        adapter=FakeVisionAdapter([_verdict_result()]),
        model="m",
        soft_cap_usd=5.0,
        month_prefix="2026-08",
    )
    assert out["scored"] == 1
    rebuild_pipeline(c)
    row = c.execute(
        "SELECT subject FROM proj_scores WHERE user_id=? AND photo_id=?", (USER, "photo-42")
    ).fetchone()
    assert row["subject"] == "trail runner"
