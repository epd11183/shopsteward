"""Derived read models for the editing module, rebuilt from the event log.

Mirrors the drop-and-rebuild style of core/projections.py but with its own
schema and its own rebuild entrypoint (rebuild_editing) so the two projection
sets stay independent.
"""

import json
import sqlite3

from shopsteward.core.events import read_all

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_photos;
CREATE TABLE proj_photos (
    user_id INTEGER NOT NULL, photo_id TEXT NOT NULL, base_name TEXT NOT NULL,
    raw_path TEXT NOT NULL, jpeg_path TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
    mode TEXT NOT NULL, status TEXT NOT NULL, exif_json TEXT NOT NULL,
    PRIMARY KEY (user_id, photo_id)
);
DROP TABLE IF EXISTS proj_ingest_jobs;
CREATE TABLE proj_ingest_jobs (
    user_id INTEGER NOT NULL, ingest_job_id TEXT NOT NULL, path TEXT NOT NULL,
    mode TEXT NOT NULL, paired INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0, unpaired INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    PRIMARY KEY (user_id, ingest_job_id)
);
DROP TABLE IF EXISTS proj_edit_jobs;
CREATE TABLE proj_edit_jobs (
    user_id INTEGER NOT NULL, edit_job_id TEXT NOT NULL, preset_family TEXT NOT NULL,
    mode TEXT NOT NULL, photo_count INTEGER NOT NULL, status TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY (user_id, edit_job_id)
);
DROP TABLE IF EXISTS proj_preset_families;
CREATE TABLE proj_preset_families (
    user_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
"""


def rebuild_editing(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)

    # (user_id, edit_job_id) -> list of photo_ids from editjob.dispatched
    dispatched_photo_ids: dict[tuple[int, str], list[str]] = {}
    # (user_id, photo_id) -> base_name, for resolving skipped[] (keyed by base_name)
    base_name_by_photo: dict[tuple[int, str], str] = {}

    for e in read_all(conn):
        p = e.payload

        if e.type == "photo.ingested":
            base_name = p.get("base_name") or p["raw_path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            base_name_by_photo[(e.user_id, p["photo_id"])] = base_name
            conn.execute(
                "INSERT OR REPLACE INTO proj_photos VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    e.user_id,
                    p["photo_id"],
                    base_name,
                    p["raw_path"],
                    p["jpeg_path"],
                    p["raw_sha256"],
                    p["mode"],
                    p["status"],
                    json.dumps(p.get("exif", {})),
                ),
            )

        elif e.type == "ingest.started":
            conn.execute(
                "INSERT OR REPLACE INTO proj_ingest_jobs VALUES (?,?,?,?,0,0,0,'started')",
                (e.user_id, p["ingest_job_id"], p["path"], p["mode"]),
            )

        elif e.type == "ingest.completed":
            conn.execute(
                "UPDATE proj_ingest_jobs SET paired=?, duplicates=?, unpaired=?, "
                "status='completed' WHERE user_id=? AND ingest_job_id=?",
                (p["paired"], p["duplicates"], p["unpaired"], e.user_id, p["ingest_job_id"]),
            )

        elif e.type == "editjob.dispatched":
            photo_ids = list(p["photo_ids"])
            dispatched_photo_ids[(e.user_id, p["edit_job_id"])] = photo_ids
            conn.execute(
                "INSERT OR REPLACE INTO proj_edit_jobs VALUES (?,?,?,?,?,'dispatched',NULL)",
                (e.user_id, p["edit_job_id"], p["preset_family"], p["mode"], len(photo_ids)),
            )
            conn.executemany(
                "UPDATE proj_photos SET status='editing' WHERE user_id=? AND photo_id=?",
                [(e.user_id, photo_id) for photo_id in photo_ids],
            )

        elif e.type == "editjob.completed":
            photo_ids = dispatched_photo_ids.get((e.user_id, p["edit_job_id"]), [])
            skipped_base_names = {s["base_name"] for s in p.get("skipped", [])}
            for photo_id in photo_ids:
                base_name = base_name_by_photo.get((e.user_id, photo_id))
                status = "edit_failed" if base_name in skipped_base_names else "edited"
                conn.execute(
                    "UPDATE proj_photos SET status=? WHERE user_id=? AND photo_id=?",
                    (status, e.user_id, photo_id),
                )
            conn.execute(
                "UPDATE proj_edit_jobs SET status='completed' WHERE user_id=? AND edit_job_id=?",
                (e.user_id, p["edit_job_id"]),
            )

        elif e.type == "editjob.failed":
            edit_job_id = p.get("edit_job_id")
            if edit_job_id is None:
                continue
            error_json = json.dumps(p.get("error", {}))
            conn.execute(
                "UPDATE proj_edit_jobs SET status='failed', error=? "
                "WHERE user_id=? AND edit_job_id=?",
                (error_json, e.user_id, edit_job_id),
            )
            photo_ids = dispatched_photo_ids.get((e.user_id, edit_job_id), [])
            conn.executemany(
                "UPDATE proj_photos SET status='edit_failed' WHERE user_id=? AND photo_id=?",
                [(e.user_id, photo_id) for photo_id in photo_ids],
            )

        elif e.type in ("presetfamily.seeded", "presetfamily.updated"):
            conn.execute(
                "INSERT OR REPLACE INTO proj_preset_families VALUES (?,?,?,?)",
                (e.user_id, p["name"], p.get("description", ""), json.dumps(p["settings"])),
            )

    conn.commit()
