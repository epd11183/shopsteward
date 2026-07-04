"""Derived read models for the mockups module: proj_staging_templates,
proj_mockups, proj_mockup_sets. Drop-and-rebuild, own schema, own rebuild
entrypoint (rebuild_mockups), mirroring pipeline/projections.py.
"""

import json
import sqlite3

from shopsteward.core.events import read_all

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_staging_templates;
CREATE TABLE proj_staging_templates (
    user_id INTEGER NOT NULL, template_id TEXT NOT NULL,
    image_path TEXT, sidecar_path TEXT, sidecar_hash TEXT,
    room_type TEXT, style TEXT, lighting TEXT, orientation TEXT,
    region_count INTEGER, avg_hue REAL, tags_json TEXT, source TEXT,
    status TEXT NOT NULL, reason TEXT,
    PRIMARY KEY (user_id, template_id)
);
DROP TABLE IF EXISTS proj_mockups;
CREATE TABLE proj_mockups (
    user_id INTEGER NOT NULL, path TEXT NOT NULL,
    photo_id TEXT, landing_file_id TEXT, set_key TEXT, intent TEXT,
    template_id TEXT, params_json TEXT, created_at TEXT,
    PRIMARY KEY (user_id, path)
);
DROP TABLE IF EXISTS proj_mockup_sets;
CREATE TABLE proj_mockup_sets (
    user_id INTEGER NOT NULL, set_key TEXT NOT NULL,
    photo_id TEXT, landing_file_id TEXT, count INTEGER, completed_at TEXT,
    PRIMARY KEY (user_id, set_key)
);
"""


def rebuild_mockups(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)

    for e in read_all(conn):
        p = e.payload

        if e.type in ("stagingtemplate.registered", "stagingtemplate.updated"):
            conn.execute(
                "INSERT OR REPLACE INTO proj_staging_templates VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,'valid',NULL)",
                (
                    e.user_id,
                    p["template_id"],
                    p["image_path"],
                    p["sidecar_path"],
                    p["sidecar_hash"],
                    p["room_type"],
                    p["style"],
                    p["lighting"],
                    p["orientation"],
                    p["region_count"],
                    p["avg_hue"],
                    json.dumps(p.get("tags", [])),
                    p["source"],
                ),
            )

        elif e.type == "stagingtemplate.invalid":
            # template_id present only when the sidecar parsed enough to identify
            # itself (e.g. duplicate_id, geometry failures); a synthetic
            # sidecar-path key covers parse-level failures, mirroring the
            # landing.py invalid-file_id fallback.
            key = p.get("template_id") or f"invalid:{p['sidecar_path']}"
            existing = conn.execute(
                "SELECT 1 FROM proj_staging_templates WHERE user_id=? AND template_id=?",
                (e.user_id, key),
            ).fetchone()
            if existing:
                # invalid after registered flips status, keeps prior metadata.
                conn.execute(
                    "UPDATE proj_staging_templates SET status='invalid', reason=? "
                    "WHERE user_id=? AND template_id=?",
                    (p.get("reason"), e.user_id, key),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO proj_staging_templates VALUES "
                    "(?,?,NULL,?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'invalid',?)",
                    (e.user_id, key, p.get("sidecar_path"), p.get("sidecar_hash"), p.get("reason")),
                )

        elif e.type == "mockup.generated":
            conn.execute(
                "INSERT OR REPLACE INTO proj_mockups VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    e.user_id,
                    p["path"],
                    p.get("photo_id"),
                    p["landing_file_id"],
                    p["set_key"],
                    p["intent"],
                    p.get("template_id"),
                    json.dumps(p.get("params", {})),
                    e.created_at,
                ),
            )

        elif e.type == "mockupset.completed":
            conn.execute(
                "INSERT OR REPLACE INTO proj_mockup_sets VALUES (?,?,?,?,?,?)",
                (
                    e.user_id,
                    p["set_key"],
                    p.get("photo_id"),
                    p["landing_file_id"],
                    p["count"],
                    e.created_at,
                ),
            )

    conn.commit()
