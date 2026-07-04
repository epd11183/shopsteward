import json
from pathlib import Path

import pytest

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.adapters.lightroom.fake import FakeBridge
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.editing import presets
from shopsteward.editing.dispatch import dispatch_edit_job
from shopsteward.editing.outcomes import scan_outcomes

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults" / "preset_families"

EDITING_DEFAULTS = {
    "naming_template": "{event}-{seq:04}",
    "event_output_root": "data/deliveries",
    "jpeg_quality": 92,
}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    presets.seed(c, user_id=1, defaults_dir=DEFAULTS_DIR)
    return c


def _ingest_photo(conn, user_id, photo_id, ingest_job_id="ij-1", base_name="IMG_1"):
    append(
        conn,
        Event(
            user_id=user_id,
            type="photo.ingested",
            payload={
                "photo_id": photo_id,
                "ingest_job_id": ingest_job_id,
                "base_name": base_name,
                "raw_path": f"C:\\shoot\\{base_name}.CR3",
                "jpeg_path": f"C:\\shoot\\{base_name}.jpg",
                "raw_sha256": photo_id,
                "exif": {},
                "mode": "mass",
                "status": "queued_for_edit",
            },
        ),
    )


def test_dispatch_mass_writes_job_file_with_export(conn, tmp_path):
    _ingest_photo(conn, 1, "p1", base_name="IMG_1")
    _ingest_photo(conn, 1, "p2", base_name="IMG_2")
    bridge_root = tmp_path / "bridge"
    bridge = FolderBridge(bridge_root)

    job = dispatch_edit_job(
        conn,
        1,
        bridge,
        photo_ids=["p1", "p2"],
        preset_family="neutral",
        mode="mass",
        event_name="testev",
        output_folder=str(tmp_path / "out"),
        editing_defaults=EDITING_DEFAULTS,
    )

    job_file = bridge_root / "jobs" / f"edit_{job.job_id}.json"
    assert job_file.exists()
    payload = json.loads(job_file.read_text())

    assert payload["schema"] == "shopsteward.editjob/1"
    assert payload["preset_family"] == "neutral"
    assert payload["develop_settings"] == {"Contrast2012": 0, "Vibrance": 0}
    assert "/" in payload["photos"][0]["raw_path"]
    assert "\\" not in payload["photos"][0]["raw_path"]
    assert payload["export"]["naming_template"] == "{event}-{seq:04}"
    assert payload["export"]["event"] == "testev"
    assert payload["export"]["jpeg_quality"] == 92
    assert "\\" not in payload["export"]["output_folder"]

    [dispatched] = read_all(conn, "editjob.dispatched")
    assert dispatched.payload["edit_job_id"] == job.job_id
    assert dispatched.payload["photo_ids"] == ["p1", "p2"]
    assert dispatched.payload["mode"] == "mass"


def test_dispatch_hero_has_no_export(conn, tmp_path):
    _ingest_photo(conn, 1, "p1")
    bridge = FolderBridge(tmp_path / "bridge")

    job = dispatch_edit_job(
        conn,
        1,
        bridge,
        photo_ids=["p1"],
        preset_family="neutral",
        mode="hero",
        event_name=None,
        output_folder=None,
        editing_defaults=EDITING_DEFAULTS,
    )

    assert job.export is None
    job_file = tmp_path / "bridge" / "jobs" / f"edit_{job.job_id}.json"
    payload = json.loads(job_file.read_text())
    assert payload["export"] is None

    [dispatched] = read_all(conn, "editjob.dispatched")
    assert dispatched.payload["export"] is None


def test_fake_bridge_consumes_mass_job_and_exports_named_files(conn, tmp_path):
    _ingest_photo(conn, 1, "p1", base_name="IMG_1")
    _ingest_photo(conn, 1, "p2", base_name="IMG_2")
    bridge_root = tmp_path / "bridge"
    out_dir = tmp_path / "out"
    bridge = FolderBridge(bridge_root)

    dispatch_edit_job(
        conn,
        1,
        bridge,
        photo_ids=["p1", "p2"],
        preset_family="neutral",
        mode="mass",
        event_name="testev",
        output_folder=str(out_dir),
        editing_defaults=EDITING_DEFAULTS,
    )

    FakeBridge(bridge_root).consume_all()

    done_dir = bridge_root / "jobs" / "done"
    done_files = [p for p in done_dir.glob("*.json") if not p.name.endswith(".result.json")]
    assert len(done_files) == 1
    result_files = list((bridge_root / "jobs" / "done").glob("*.result.json"))
    assert len(result_files) == 1

    exported_names = sorted(p.name for p in out_dir.glob("*.jpg"))
    assert exported_names == ["testev-0001.jpg", "testev-0002.jpg"]


def test_scan_outcomes_is_idempotent(conn, tmp_path):
    _ingest_photo(conn, 1, "p1")
    bridge_root = tmp_path / "bridge"
    bridge = FolderBridge(bridge_root)

    dispatch_edit_job(
        conn,
        1,
        bridge,
        photo_ids=["p1"],
        preset_family="neutral",
        mode="mass",
        event_name="testev",
        output_folder=str(tmp_path / "out"),
        editing_defaults=EDITING_DEFAULTS,
    )
    FakeBridge(bridge_root).consume_all()

    new_count = scan_outcomes(conn, 1, bridge)
    assert new_count == 1
    [completed] = read_all(conn, "editjob.completed")
    assert completed.payload["applied"] == 1

    second_count = scan_outcomes(conn, 1, bridge)
    assert second_count == 0
    assert len(read_all(conn, "editjob.completed")) == 1


def test_scan_outcomes_records_forced_failure(conn, tmp_path):
    _ingest_photo(conn, 1, "p1")
    bridge_root = tmp_path / "bridge"
    bridge = FolderBridge(bridge_root)

    job = dispatch_edit_job(
        conn,
        1,
        bridge,
        photo_ids=["p1"],
        preset_family="neutral",
        mode="mass",
        event_name="testev",
        output_folder=str(tmp_path / "out"),
        editing_defaults=EDITING_DEFAULTS,
    )

    job_file = bridge_root / "jobs" / f"edit_{job.job_id}.json"
    payload = json.loads(job_file.read_text())
    payload["_force_fail"] = True
    job_file.write_text(json.dumps(payload))

    FakeBridge(bridge_root).consume_all()
    new_count = scan_outcomes(conn, 1, bridge)

    assert new_count == 1
    [failed] = read_all(conn, "editjob.failed")
    assert failed.payload["error"]["code"] == "apply_error"
    assert (bridge_root / "jobs" / "failed" / f"edit_{job.job_id}.json").exists()
