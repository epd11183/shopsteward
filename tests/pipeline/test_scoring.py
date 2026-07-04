import json
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from shopsteward.adapters.vision.fake import FakeVisionAdapter
from shopsteward.adapters.vision.interface import VisionResult, VisionVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.editing.ingest import ingest_folder
from shopsteward.pipeline import tuning
from shopsteward.pipeline.scoring import run_scoring

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _make_jpeg(path: Path) -> None:
    Image.new("RGB", (50, 50), color=(120, 130, 140)).save(path, "JPEG")


def _make_raw(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _ingest_photos(conn, tmp_path, names: list[str]) -> list[str]:
    folder = tmp_path / "shoot"
    folder.mkdir(exist_ok=True)
    for name in names:
        _make_raw(folder / f"{name}.CR3", content=f"raw-{name}".encode())
        _make_jpeg(folder / f"{name}.jpg")
    report = ingest_folder(conn, user_id=1, path=folder, mode="hero")
    return report.photo_ids


def _verdict_result(score: int) -> VisionResult:
    return VisionResult(
        verdict=VisionVerdict(
            commercial_score=score,
            subject="lake house",
            strongest_room_style="coastal",
            one_risk="none flagged",
            rationale=f"test verdict at score {score}",
        )
    )


def _seed_commercial_only_profile(conn, tmp_path):
    """Zero out the technical weight so composite == commercial exactly,
    making borderline/escalation outcomes deterministic regardless of the
    (unpredictable, content-hash-ordered) candidate processing order."""
    profile_dict = json.loads(DEFAULTS_PATH.read_text())
    profile_dict["scoring"]["weights"] = {
        "technical": 0.0,
        "commercial": 1.0,
        "catalog_gap": 0.0,
        "historical_conversion": 0.0,
    }
    path = tmp_path / "commercial_only_profile.json"
    path.write_text(json.dumps(profile_dict))
    tuning.seed(conn, user_id=1, path=path)


def test_scoring_run_queues_pass_and_escalates_borderline(conn, tmp_path):
    _ingest_photos(conn, tmp_path, ["a", "b", "c"])
    _seed_commercial_only_profile(conn, tmp_path)

    vision = FakeVisionAdapter(
        results=[
            _verdict_result(85),  # clear pass, no escalation
            _verdict_result(20),  # clear fail, no escalation
            _verdict_result(58),  # borderline (threshold 60, band 10) -> rescore
            _verdict_result(75),  # rescore replaces the borderline score -> queued
        ]
    )

    result = run_scoring(conn, user_id=1, vision=vision, live=False)

    assert result.scored == 3
    assert result.queued == 2
    assert result.escalated == 1
    assert result.failed == 0
    assert result.cap_hit is False

    queued_events = read_all(conn, "photo.queued")
    assert len(queued_events) == 2

    scored_events = read_all(conn, "photo.scored")
    assert len(scored_events) == 3
    escalated_events = [e for e in scored_events if e.payload["escalated"]]
    assert len(escalated_events) == 1
    assert escalated_events[0].payload["composite"] == 75.0

    rows = conn.execute("SELECT * FROM proj_scores WHERE user_id=1").fetchall()
    assert len(rows) == 3
    assert all(row["rationale"] for row in rows)

    assert read_all(conn, "llm.call") == []  # fixture/fake mode: no usage -> no cost events


def test_second_run_is_idempotent(conn, tmp_path):
    _ingest_photos(conn, tmp_path, ["a", "b", "c"])
    _seed_commercial_only_profile(conn, tmp_path)

    vision = FakeVisionAdapter(
        results=[
            _verdict_result(85),
            _verdict_result(20),
            _verdict_result(58),
            _verdict_result(75),
        ]
    )
    first = run_scoring(conn, user_id=1, vision=vision, live=False)
    assert first.scored == 3

    second = run_scoring(conn, user_id=1, vision=FakeVisionAdapter(results=[]), live=False)
    assert second.scored == 0
    assert second.queued == 0
    assert second.failed == 0


def test_scorer_exception_stays_eligible_for_next_run(conn, tmp_path):
    _ingest_photos(conn, tmp_path, ["a"])
    # Commercial-only weighting avoids the tiny test JPEG's resolution-guard-capped
    # technical score accidentally landing the composite in the borderline band.
    _seed_commercial_only_profile(conn, tmp_path)

    failing_vision = FakeVisionAdapter(results=[RuntimeError("gemini blew up")])
    first = run_scoring(conn, user_id=1, vision=failing_vision, live=False)

    assert first.scored == 0
    assert first.failed == 1
    failed_events = read_all(conn, "photo.score_failed")
    assert len(failed_events) == 1
    assert failed_events[0].payload["scorer"] == "commercial"
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM proj_scores WHERE user_id=1").fetchone()["n"] == 0
    )

    recovering_vision = FakeVisionAdapter(results=[_verdict_result(85)])
    second = run_scoring(conn, user_id=1, vision=recovering_vision, live=False)

    assert second.scored == 1
    assert second.failed == 0


def test_live_gate_cli_refuses_without_env(monkeypatch):
    monkeypatch.delenv("SHOPSTEWARD_LIVE_VISION", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    from shopsteward.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["score", "run", "--live-vision"])

    assert result.exit_code == 1
    assert "PRD" in result.output
