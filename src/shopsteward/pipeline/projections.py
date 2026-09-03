"""Derived read models for the pipeline module: proj_tuning_profiles,
proj_scores, proj_landing_files. Drop-and-rebuild, own schema, own
rebuild entrypoint (rebuild_pipeline), mirroring editing/projections.py.

Ownership rule: pipeline never writes proj_photos (owned by editing).

Note: automated Etsy gating (vision scoring + Gate 1 curation) was ripped out
-- see 2026-08-10-ripout-etsy-gating. proj_scores is kept because
pipeline/listings/copy.py still reads it for vision-verdict copy signals, but
it has no live producer anymore: it only projects historical/seeded
`photo.scored` events. proj_gate1 had no kept-side consumer and was removed
along with the gate1.* event handlers.

Winners-batch reset (pipeline/listings/reset.py) adds landing.file_reset,
but this projection has NO fold for it -- reviewer finding: the DELETE this
module used to run for it was dead code, since a re-observe's
landing.file_observed already folds via INSERT OR REPLACE and overwrites
the row regardless. The load-bearing half of a reset lives entirely in
landing.py's `_known_file_ids()` ordered fold, which discards the file_id on
landing.file_reset so scan_landing treats it as unseen and re-observes it.
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
DROP TABLE IF EXISTS proj_landing_files;
CREATE TABLE proj_landing_files (
    user_id INTEGER NOT NULL, file_id TEXT NOT NULL,
    path TEXT NOT NULL, base_name TEXT, photo_id TEXT,
    format TEXT, width INTEGER, height INTEGER, color_space TEXT,
    status TEXT NOT NULL, reason TEXT,
    PRIMARY KEY (user_id, file_id)
);
"""


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
            # file_id present since 2026-07-04 (bytes are always hashable);
            # path-key fallback covers events recorded before that.
            conn.execute(
                "INSERT OR REPLACE INTO proj_landing_files VALUES "
                "(?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,'invalid',?)",
                (e.user_id, p.get("file_id") or f"invalid:{p['path']}", p["path"], p.get("reason")),
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
