"""Derived read models for the listings module: proj_listing_config,
proj_listing_drafts. Drop-and-rebuild, own schema, own rebuild entrypoint
(rebuild_listings), mirroring mockups/projections.py.

Ownership rule: listings never writes proj_photos, proj_landing_files, or
proj_mockups (owned by editing/pipeline/mockups respectively).

Slice 1 folds only listingconfig.seeded/.updated + listingdraft.created +
.images_selected -- copy/pricing/push/Gate 3 events are folded in later
M5a slices when those events start being emitted.
"""

import json
import sqlite3

from shopsteward.core.events import read_all

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_listing_config;
CREATE TABLE proj_listing_config (
    user_id INTEGER NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
DROP TABLE IF EXISTS proj_listing_drafts;
CREATE TABLE proj_listing_drafts (
    user_id INTEGER NOT NULL, draft_id TEXT NOT NULL,
    landing_file_id TEXT, photo_id TEXT, set_key TEXT,
    provider TEXT, format TEXT, sku_source TEXT, listing_type TEXT,
    config_hash TEXT, etsy_listing_id TEXT,
    title TEXT, tags_json TEXT NOT NULL DEFAULT '[]', description TEXT,
    price REAL, currency TEXT, margin_floor REAL,
    images_json TEXT NOT NULL DEFAULT '[]', file_source TEXT,
    state TEXT NOT NULL, created_at TEXT, published_at TEXT,
    PRIMARY KEY (user_id, draft_id)
);
"""


def rebuild_listings(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)

    for e in read_all(conn):
        p = e.payload

        if e.type in ("listingconfig.seeded", "listingconfig.updated"):
            conn.execute(
                "INSERT OR REPLACE INTO proj_listing_config VALUES (?,?,?)",
                (e.user_id, p["name"], json.dumps(p["config"])),
            )

        elif e.type == "listingdraft.created":
            conn.execute(
                "INSERT OR REPLACE INTO proj_listing_drafts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,NULL,NULL,'[]',NULL,NULL,NULL,NULL,'[]',NULL,'built',?,NULL)",
                (
                    e.user_id,
                    p["draft_id"],
                    p["landing_file_id"],
                    p.get("photo_id"),
                    p["set_key"],
                    p["provider"],
                    p["format"],
                    p["sku_source"],
                    p["listing_type"],
                    p["config_hash"],
                    e.created_at,
                ),
            )

        elif e.type == "listingdraft.images_selected":
            conn.execute(
                "UPDATE proj_listing_drafts SET images_json=?, file_source=? "
                "WHERE user_id=? AND draft_id=?",
                (
                    json.dumps(p["images"]),
                    p["sellable_file"]["source"],
                    e.user_id,
                    p["draft_id"],
                ),
            )

    conn.commit()
