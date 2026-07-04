"""Gate 1 decisions: approve dispatches a hero edit job, undo best-effort
recalls an unconsumed job, reject/snooze/requeue fold correctly, and
double-deciding an already-decided photo raises."""

import json
from pathlib import Path

import pytest
from PIL import Image

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.adapters.lightroom.fake import FakeBridge
from shopsteward.adapters.vision.fake import FakeVisionAdapter
from shopsteward.adapters.vision.interface import VisionResult, VisionVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.editing import presets
from shopsteward.editing.config import PRESET_FAMILIES_DIR
from shopsteward.editing.ingest import ingest_folder
from shopsteward.pipeline import gate1, tuning
from shopsteward.pipeline.scoring import run_scoring

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    presets.seed(c, user_id=1, defaults_dir=PRESET_FAMILIES_DIR)
    return c


def _make_jpeg(path: Path) -> None:
    Image.new("RGB", (50, 50), color=(100, 110, 120)).save(path, "JPEG")


def _make_raw(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _seed_commercial_only_profile(conn, tmp_path):
    """Zero out the technical weight so composite == commercial exactly,
    making the tiny test JPEGs' resolution-guard-capped technical score
    irrelevant to whether the photo clears the Gate 1 threshold."""
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


def _queue_one_photo(conn, tmp_path, name: str = "hero1", score: int = 85) -> str:
    """Ingest one hero photo and score it high enough to land in proj_gate1."""
    folder = tmp_path / "shoot"
    folder.mkdir(exist_ok=True)
    _make_raw(folder / f"{name}.CR3", f"raw-{name}".encode())
    _make_jpeg(folder / f"{name}.jpg")
    report = ingest_folder(conn, user_id=1, path=folder, mode="hero")
    photo_id = report.photo_ids[0]

    _seed_commercial_only_profile(conn, tmp_path)
    vision = FakeVisionAdapter(
        results=[
            VisionResult(
                verdict=VisionVerdict(
                    commercial_score=score,
                    subject="lake house",
                    strongest_room_style="coastal",
                    one_risk="none flagged",
                    rationale="test verdict",
                )
            )
        ]
    )
    run_scoring(conn, user_id=1, vision=vision, live=False)
    return photo_id


def test_queue_returns_pending_cards(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)

    queue = gate1.get_queue(conn, user_id=1, state="pending")

    assert len(queue) == 1
    card = queue[0]
    assert card.photo_id == photo_id
    assert card.state == "pending"
    assert card.base_name == "hero1"
    assert card.subject == "lake house"
    assert card.edit_job_id is None
    assert card.dispatch_state is None


def test_approve_dispatches_hero_job(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge = FolderBridge(tmp_path / "bridge")

    card = gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="approve")

    assert card.state == "approved"
    assert card.edit_job_id is not None

    dispatched = read_all(conn, "editjob.dispatched")
    assert len(dispatched) == 1
    assert dispatched[0].payload["export"] is None
    assert dispatched[0].payload["collection"] == "ShopSteward — Needs Finishing"
    assert dispatched[0].payload["mode"] == "hero"
    assert dispatched[0].payload["photo_ids"] == [photo_id]

    approved_events = read_all(conn, "gate1.approved")
    assert len(approved_events) == 1
    assert approved_events[0].payload["edit_job_id"] == dispatched[0].payload["edit_job_id"]
    assert approved_events[0].payload["photo_id"] == photo_id

    job_file = tmp_path / "bridge" / "jobs" / f"edit_{card.edit_job_id}.json"
    assert job_file.exists()


def test_undo_recalls_unconsumed_job(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge_root = tmp_path / "bridge"
    bridge = FolderBridge(bridge_root)

    card = gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="approve")
    job_file = bridge_root / "jobs" / f"edit_{card.edit_job_id}.json"
    assert job_file.exists()

    result = gate1.undo(conn, user_id=1, bridge=bridge, photo_id=photo_id)

    assert result == {"photo_id": photo_id, "undo_of": "approved", "job_recalled": True}
    assert not job_file.exists()

    undone_events = read_all(conn, "gate1.undone")
    assert len(undone_events) == 1
    assert undone_events[0].payload == result

    refreshed = gate1.get_queue(conn, user_id=1, state="pending")
    assert any(c.photo_id == photo_id and c.edit_job_id is None for c in refreshed)


def test_undo_after_consume_reports_not_recalled(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge_root = tmp_path / "bridge"
    bridge = FolderBridge(bridge_root)

    gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="approve")
    FakeBridge(bridge_root).consume_all()  # Lua consumer picks it up: job moves to jobs/done/

    result = gate1.undo(conn, user_id=1, bridge=bridge, photo_id=photo_id)

    assert result["job_recalled"] is False
    assert result["undo_of"] == "approved"


def test_reject_folds_state_and_double_decide_raises(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge = FolderBridge(tmp_path / "bridge")

    card = gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="reject")
    assert card.state == "rejected"
    assert read_all(conn, "gate1.rejected")[0].payload == {"photo_id": photo_id}

    with pytest.raises(ValueError):
        gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="reject")

    with pytest.raises(ValueError):
        gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="approve")


def test_snooze_then_requeue_returns_to_pending(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge = FakeBridge(tmp_path / "bridge")

    snoozed = gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="snooze")
    assert snoozed.state == "snoozed"
    assert gate1.get_queue(conn, user_id=1, state="snoozed")[0].photo_id == photo_id

    requeued = gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="requeue")
    assert requeued.state == "pending"

    undone = read_all(conn, "gate1.undone")
    assert undone[-1].payload["undo_of"] == "snoozed"
    assert undone[-1].payload["job_recalled"] is False


def test_requeue_from_pending_raises(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge = FakeBridge(tmp_path / "bridge")

    with pytest.raises(ValueError):
        gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="requeue")


def test_invalid_decision_raises(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge = FakeBridge(tmp_path / "bridge")

    with pytest.raises(ValueError):
        gate1.decide(conn, user_id=1, bridge=bridge, photo_id=photo_id, decision="nonsense")


def test_decide_unknown_photo_raises(conn, tmp_path):
    bridge = FakeBridge(tmp_path / "bridge")
    with pytest.raises(ValueError):
        gate1.decide(conn, user_id=1, bridge=bridge, photo_id="does-not-exist", decision="approve")


def test_undo_unknown_photo_raises(conn, tmp_path):
    bridge = FakeBridge(tmp_path / "bridge")
    with pytest.raises(ValueError):
        gate1.undo(conn, user_id=1, bridge=bridge, photo_id="does-not-exist")


def test_undo_pending_photo_raises(conn, tmp_path):
    photo_id = _queue_one_photo(conn, tmp_path)
    bridge = FakeBridge(tmp_path / "bridge")
    with pytest.raises(ValueError):
        gate1.undo(conn, user_id=1, bridge=bridge, photo_id=photo_id)
