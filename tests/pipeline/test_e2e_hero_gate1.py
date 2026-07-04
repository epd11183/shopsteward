"""E2E (design §7, zero network): hero ingest 3 -> score run with FakeVision
(pass/fail/borderline w/ Pro rescore) -> events + projections -> gate1 approve
w/ a real FolderBridge -> hero editjob.dispatched (export None, Needs
Finishing) -> landing scan (match + manual drop + invalid) -> zero llm.call
events throughout."""

import json
from pathlib import Path

import pytest
from PIL import Image

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.adapters.vision.fake import FakeVisionAdapter
from shopsteward.adapters.vision.interface import VisionResult, VisionVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.editing import presets
from shopsteward.editing.config import PRESET_FAMILIES_DIR
from shopsteward.editing.ingest import ingest_folder
from shopsteward.pipeline import gate1, landing, tuning
from shopsteward.pipeline.scoring import run_scoring

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    presets.seed(c, user_id=1, defaults_dir=PRESET_FAMILIES_DIR)
    return c


def _verdict(score: int) -> VisionResult:
    return VisionResult(
        verdict=VisionVerdict(
            commercial_score=score,
            subject="lake house",
            strongest_room_style="coastal",
            one_risk="none flagged",
            rationale=f"score {score}",
        )
    )


def test_hero_ingest_score_approve_landing_e2e(conn, tmp_path):
    # 1. hero ingest 3.
    shoot = tmp_path / "shoot"
    shoot.mkdir()
    for name in ("pass_photo", "fail_photo", "borderline_photo"):
        (shoot / f"{name}.CR3").write_bytes(f"raw-{name}".encode())
        Image.new("RGB", (50, 50), (120, 130, 140)).save(shoot / f"{name}.jpg", "JPEG")

    report = ingest_folder(conn, user_id=1, path=shoot, mode="hero")
    assert report.paired == 3

    # Commercial-only weighting: deterministic composite regardless of the
    # tiny test JPEGs' resolution-guard-capped technical score.
    profile_dict = json.loads(DEFAULTS_PATH.read_text())
    profile_dict["scoring"]["weights"] = {
        "technical": 0.0,
        "commercial": 1.0,
        "catalog_gap": 0.0,
        "historical_conversion": 0.0,
    }
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile_dict))
    tuning.seed(conn, user_id=1, path=profile_path)

    # 2. score run: pass / fail / borderline+rescore (fixture/fake mode, no network).
    vision = FakeVisionAdapter(
        results=[
            _verdict(85),  # pass_photo: clear pass
            _verdict(20),  # fail_photo: clear fail
            _verdict(58),  # borderline_photo: within threshold(60) +/- band(10) -> rescore
            _verdict(75),  # rescore replaces the borderline score -> queued
        ]
    )
    result = run_scoring(conn, user_id=1, vision=vision, live=False)
    assert result.scored == 3
    assert result.queued == 2
    assert result.escalated == 1
    assert result.failed == 0
    assert read_all(conn, "llm.call") == []  # fixture/fake mode never emits llm.call

    # 3. approve the top card via gate1.decide with a real FolderBridge.
    queue = gate1.get_queue(conn, user_id=1, state="pending")
    assert len(queue) == 2
    top, second = queue
    assert top.composite >= second.composite

    bridge = FolderBridge(tmp_path / "bridge")
    card = gate1.decide(conn, user_id=1, bridge=bridge, photo_id=top.photo_id, decision="approve")
    assert card.state == "approved"
    assert card.edit_job_id is not None

    dispatched = read_all(conn, "editjob.dispatched")
    assert len(dispatched) == 1
    assert dispatched[0].payload["mode"] == "hero"
    assert dispatched[0].payload["export"] is None
    assert dispatched[0].payload["collection"] == "ShopSteward — Needs Finishing"

    job_file = tmp_path / "bridge" / "jobs" / f"edit_{card.edit_job_id}.json"
    assert job_file.exists()

    # 4. landing scan: a matching TIFF, an orphan JPEG (manual drop), and an
    # undersized JPEG (invalid).
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    Image.new("RGB", (3500, 3600)).save(landing_dir / f"{top.base_name}.tif", "TIFF")
    Image.new("RGB", (3200, 3200)).save(landing_dir / "orphan_shot.jpg", "JPEG")
    Image.new("RGB", (100, 100)).save(landing_dir / "too_small.jpg", "JPEG")

    landing_report = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)
    assert landing_report.observed == 2
    assert landing_report.matched == 1
    assert landing_report.manual_drops == 1
    assert landing_report.invalid == 1

    # Zero network calls end to end.
    assert read_all(conn, "llm.call") == []
