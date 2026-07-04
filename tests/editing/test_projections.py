import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.editing.projections import rebuild_editing


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _ingest_photo(conn, user_id, photo_id, base_name, status):
    append(
        conn,
        Event(
            user_id=user_id,
            type="photo.ingested",
            payload={
                "photo_id": photo_id,
                "ingest_job_id": "ing_1",
                "base_name": base_name,
                "raw_path": f"C:/raws/{base_name}.CR3",
                "jpeg_path": f"C:/raws/{base_name}.jpg",
                "raw_sha256": photo_id,
                "exif": {},
                "mode": "mass",
                "status": status,
            },
        ),
    )


def test_edit_job_completed_marks_edited_and_edit_failed(conn):
    _ingest_photo(conn, 1, "photo_a", "IMG_1", "queued_for_edit")
    _ingest_photo(conn, 1, "photo_b", "IMG_2", "queued_for_edit")

    append(
        conn,
        Event(
            user_id=1,
            type="editjob.dispatched",
            payload={
                "edit_job_id": "job_1",
                "ingest_job_id": "ing_1",
                "photo_ids": ["photo_a", "photo_b"],
                "preset_family": "wedding",
                "mode": "mass",
                "collection": "ShopSteward — test",
                "job_file": "edit_job_1.json",
                "export": {
                    "output_folder": "C:/out",
                    "naming_template": "{event}-{seq:04}",
                    "event": "test-event",
                },
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=1,
            type="editjob.completed",
            payload={
                "edit_job_id": "job_1",
                "applied": 1,
                "skipped": [{"base_name": "IMG_2", "reason": "not_in_catalog"}],
                "exported": ["test-event-0001"],
                "finished_at": "2026-07-03T00:00:00Z",
            },
        ),
    )

    rebuild_editing(conn)

    rows = {
        r["photo_id"]: r["status"]
        for r in conn.execute("SELECT photo_id, status FROM proj_photos WHERE user_id=1")
    }
    assert rows["photo_a"] == "edited"
    assert rows["photo_b"] == "edit_failed"

    job = conn.execute(
        "SELECT status, photo_count FROM proj_edit_jobs WHERE user_id=1 AND edit_job_id='job_1'"
    ).fetchone()
    assert job["status"] == "completed"
    assert job["photo_count"] == 2


def test_edit_job_dispatched_sets_editing_status(conn):
    _ingest_photo(conn, 1, "photo_a", "IMG_1", "queued_for_edit")
    append(
        conn,
        Event(
            user_id=1,
            type="editjob.dispatched",
            payload={
                "edit_job_id": "job_1",
                "ingest_job_id": "ing_1",
                "photo_ids": ["photo_a"],
                "preset_family": "wedding",
                "mode": "mass",
                "collection": "c",
                "job_file": "f.json",
                "export": None,
            },
        ),
    )

    rebuild_editing(conn)

    status = conn.execute(
        "SELECT status FROM proj_photos WHERE user_id=1 AND photo_id='photo_a'"
    ).fetchone()["status"]
    assert status == "editing"


def test_edit_job_failed_marks_photos_edit_failed(conn):
    _ingest_photo(conn, 1, "photo_a", "IMG_1", "queued_for_edit")
    append(
        conn,
        Event(
            user_id=1,
            type="editjob.dispatched",
            payload={
                "edit_job_id": "job_1",
                "ingest_job_id": "ing_1",
                "photo_ids": ["photo_a"],
                "preset_family": "wedding",
                "mode": "mass",
                "collection": "c",
                "job_file": "f.json",
                "export": None,
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=1,
            type="editjob.failed",
            payload={
                "edit_job_id": "job_1",
                "file_name": "edit_job_1.json",
                "error": {"code": "crash", "message": "boom"},
            },
        ),
    )

    rebuild_editing(conn)

    row = conn.execute(
        "SELECT status, error FROM proj_edit_jobs WHERE user_id=1 AND edit_job_id='job_1'"
    ).fetchone()
    assert row["status"] == "failed"
    assert "boom" in row["error"]

    photo_status = conn.execute(
        "SELECT status FROM proj_photos WHERE user_id=1 AND photo_id='photo_a'"
    ).fetchone()["status"]
    assert photo_status == "edit_failed"


def test_preset_family_projection_last_write_wins(conn):
    append(
        conn,
        Event(
            user_id=1,
            type="presetfamily.seeded",
            payload={
                "name": "wedding",
                "description": "v1",
                "settings": {"Vibrance": 18},
                "source": "defaults",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=1,
            type="presetfamily.updated",
            payload={
                "name": "wedding",
                "description": "v2",
                "settings": {"Vibrance": 99},
                "source": "bridge_export",
            },
        ),
    )

    rebuild_editing(conn)

    row = conn.execute(
        "SELECT description, settings_json FROM proj_preset_families "
        "WHERE user_id=1 AND name='wedding'"
    ).fetchone()
    assert row["description"] == "v2"
    assert '"Vibrance": 99' in row["settings_json"]


def test_ingest_job_projection_tracks_started_and_completed(conn):
    append(
        conn,
        Event(
            user_id=1,
            type="ingest.started",
            payload={"ingest_job_id": "ing_1", "path": "C:/raws", "mode": "mass"},
        ),
    )
    append(
        conn,
        Event(
            user_id=1,
            type="ingest.completed",
            payload={"ingest_job_id": "ing_1", "paired": 2, "duplicates": 1, "unpaired": 1},
        ),
    )

    rebuild_editing(conn)

    row = conn.execute(
        "SELECT status, paired, duplicates, unpaired FROM proj_ingest_jobs "
        "WHERE user_id=1 AND ingest_job_id='ing_1'"
    ).fetchone()
    assert row["status"] == "completed"
    assert (row["paired"], row["duplicates"], row["unpaired"]) == (2, 1, 1)
