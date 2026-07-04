"""Derived read models for the pipeline module: proj_tuning_profiles,
proj_scores, proj_gate1, proj_landing_files. Drop-and-rebuild, own schema, own
rebuild entrypoint (rebuild_pipeline), mirroring editing/projections.py.

Ownership rule: pipeline never writes proj_photos (owned by editing).
"""

import json
import sqlite3

from shopsteward.core.events import read_all

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_tuning_profiles;
CREATE TABLE proj_tuning_profiles (
    user_id INTEGER NOT NULL, name TEXT NOT NULL, profile_json TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
DROP TABLE IF EXISTS proj_scores;
CREATE TABLE proj_scores (
    user_id INTEGER NOT NULL, photo_id TEXT NOT NULL,
    technical REAL, commercial REAL, catalog_gap REAL, historical_conversion REAL,
    composite REAL NOT NULL, escalated INTEGER NOT NULL DEFAULT 0,
    subject TEXT, strongest_room_style TEXT, one_risk TEXT, rationale TEXT,
    model_used TEXT, scored_at TEXT,
    PRIMARY KEY (user_id, photo_id)
);
DROP TABLE IF EXISTS proj_gate1;
CREATE TABLE proj_gate1 (
    user_id INTEGER NOT NULL, photo_id TEXT NOT NULL,
    state TEXT NOT NULL, composite REAL NOT NULL, decided_at TEXT,
    edit_job_id TEXT,
    PRIMARY KEY (user_id, photo_id)
);
DROP TABLE IF EXISTS proj_landing_files;
CREATE TABLE proj_landing_files (
    user_id INTEGER NOT NULL, file_id TEXT NOT NULL,
    path TEXT NOT NULL, base_name TEXT, photo_id TEXT,
    format TEXT, width INTEGER, height INTEGER, color_space TEXT,
    status TEXT NOT NULL, reason TEXT,
    PRIMARY KEY (user_id, file_id)
);
"""

_GATE1_STATE_BY_EVENT = {
    "gate1.approved": "approved",
    "gate1.rejected": "rejected",
    "gate1.snoozed": "snoozed",
}


def rebuild_pipeline(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)

    for e in read_all(conn):
        p = e.payload

        if e.type in ("tuningprofile.seeded", "tuningprofile.updated"):
            conn.execute(
                "INSERT OR REPLACE INTO proj_tuning_profiles VALUES (?,?,?)",
                (e.user_id, p["name"], json.dumps(p["profile"])),
            )

        elif e.type == "photo.scored":
            _fold_photo_scored(conn, e.user_id, e.created_at, p)

        elif e.type == "photo.queued":
            conn.execute(
                "INSERT OR REPLACE INTO proj_gate1 VALUES (?,?,'pending',?,NULL,NULL)",
                (e.user_id, p["photo_id"], p["composite"]),
            )

        elif e.type in _GATE1_STATE_BY_EVENT:
            conn.execute(
                "UPDATE proj_gate1 SET state=?, decided_at=?, edit_job_id=? "
                "WHERE user_id=? AND photo_id=?",
                (
                    _GATE1_STATE_BY_EVENT[e.type],
                    e.created_at,
                    p.get("edit_job_id"),
                    e.user_id,
                    p["photo_id"],
                ),
            )

        elif e.type == "gate1.undone":
            conn.execute(
                "UPDATE proj_gate1 SET state='pending', decided_at=NULL, edit_job_id=NULL "
                "WHERE user_id=? AND photo_id=?",
                (e.user_id, p["photo_id"]),
            )

        elif e.type == "landing.file_observed":
            conn.execute(
                "INSERT OR REPLACE INTO proj_landing_files VALUES (?,?,?,?,?,?,?,?,?,'valid',NULL)",
                (
                    e.user_id,
                    p["file_id"],
                    p["path"],
                    p.get("base_name"),
                    p.get("photo_id"),
                    p.get("format"),
                    p.get("width"),
                    p.get("height"),
                    p.get("color_space"),
                ),
            )

        elif e.type == "landing.file_invalid":
            # landing.file_invalid carries no file_id (the file may not even
            # be hashable); key on the path so re-observing the same bad file
            # updates in place instead of accumulating duplicates.
            conn.execute(
                "INSERT OR REPLACE INTO proj_landing_files VALUES "
                "(?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,'invalid',?)",
                (e.user_id, f"invalid:{p['path']}", p["path"], p.get("reason")),
            )

    conn.commit()


def _fold_photo_scored(
    conn: sqlite3.Connection, user_id: int, created_at: str | None, p: dict
) -> None:
    vision = p.get("vision") or {}
    chosen = vision.get("rescore") or vision.get("triage") or {}
    verdict = chosen.get("verdict") or {}
    scores = p.get("scores", {})
    conn.execute(
        "INSERT OR REPLACE INTO proj_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            p["photo_id"],
            scores.get("technical"),
            scores.get("commercial"),
            scores.get("catalog_gap"),
            scores.get("historical_conversion"),
            p["composite"],
            int(p.get("escalated", False)),
            verdict.get("subject"),
            verdict.get("strongest_room_style"),
            verdict.get("one_risk"),
            verdict.get("rationale"),
            chosen.get("model"),
            created_at,
        ),
    )
