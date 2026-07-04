import json
import os
from pathlib import Path

import pytest

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.adapters.lightroom.fake import FakeBridge
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.editing import presets
from shopsteward.editing.dispatch import dispatch_edit_job
from shopsteward.editing.outcomes import scan_outcomes
from shopsteward.editing.projections import rebuild_editing

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


def test_dispatch_default_output_folder_is_absolute(conn, tmp_path):
    _ingest_photo(conn, 1, "p1")
    bridge_root = tmp_path / "bridge"
    bridge = FolderBridge(bridge_root)

    # No --out: the relative event_output_root default must be resolved.
    job = dispatch_edit_job(
        conn,
        1,
        bridge,
        photo_ids=["p1"],
        preset_family="neutral",
        mode="mass",
        event_name="testev",
        output_folder=None,
        editing_defaults=EDITING_DEFAULTS,
    )

    payload = json.loads((bridge_root / "jobs" / f"edit_{job.job_id}.json").read_text())
    folder = payload["export"]["output_folder"]
    assert os.path.isabs(folder.replace("/", os.sep)), folder


def test_dispatch_explicit_output_folder_is_absolute(conn, tmp_path):
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
        output_folder="relative/out",
        editing_defaults=EDITING_DEFAULTS,
    )

    payload = json.loads((bridge_root / "jobs" / f"edit_{job.job_id}.json").read_text())
    folder = payload["export"]["output_folder"]
    assert os.path.isabs(folder.replace("/", os.sep)), folder


def test_dispatch_empty_photo_ids_raises(conn, tmp_path):
    bridge = FolderBridge(tmp_path / "bridge")
    with pytest.raises(ValueError, match="photo_ids"):
        dispatch_edit_job(
            conn,
            1,
            bridge,
            photo_ids=[],
            preset_family="neutral",
            mode="mass",
            event_name="testev",
            output_folder=str(tmp_path / "out"),
            editing_defaults=EDITING_DEFAULTS,
        )
    assert read_all(conn, "editjob.dispatched") == []


def test_fake_bridge_malformed_dispatched_job_recovers_job_id(conn, tmp_path):
    """A corrupted dispatched job lands in failed/ with the uuid recovered
    from the filename (edit_ prefix stripped) so its proj row goes failed."""
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
    job_file.write_text("{corrupt", encoding="utf-8")

    FakeBridge(bridge_root).consume_all()

    assert (bridge_root / "jobs" / "failed" / f"edit_{job.job_id}.json").exists()
    assert scan_outcomes(conn, 1, bridge) == 1
    [failed] = read_all(conn, "editjob.failed")
    assert failed.payload["edit_job_id"] == job.job_id
    assert failed.payload["error"]["code"] == "malformed"

    rebuild_editing(conn)
    row = conn.execute(
        "SELECT status FROM proj_edit_jobs WHERE user_id=1 AND edit_job_id=?",
        (job.job_id,),
    ).fetchone()
    assert row["status"] == "failed"


def test_scan_outcomes_dedupes_results_without_job_id_by_file_name(conn, tmp_path):
    bridge_root = tmp_path / "bridge"
    failed_dir = bridge_root / "jobs" / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "junk.result.json").write_text(
        json.dumps(
            {
                "schema": "shopsteward.editresult/1",
                "status": "failed",
                "file_name": "junk.json",
                "applied": 0,
                "skipped": [],
                "exported": [],
                "error": {"code": "malformed", "message": "boom"},
                "finished_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    bridge = FolderBridge(bridge_root)

    assert scan_outcomes(conn, 1, bridge) == 1
    assert scan_outcomes(conn, 1, bridge) == 0
    assert len(read_all(conn, "editjob.failed")) == 1


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
