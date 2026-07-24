"""Derived read models for the listings module: proj_listing_config,
proj_listing_drafts. Drop-and-rebuild, own schema, own rebuild entrypoint
(rebuild_listings), mirroring mockups/projections.py.

Ownership rule: listings never writes proj_photos, proj_landing_files, or
proj_mockups (owned by editing/pipeline/mockups respectively).

Slice 1 folded listingconfig.seeded/.updated + listingdraft.created +
.images_selected. Slice 2 adds listingdraft.copy_generated (title, tags_json,
description) + listingdraft.priced (price, currency, margin_floor) + a
minimal gate3.published -> state='published' fold (the reconciled skip
predicate, design §3, needs a real published state to check against) --
push/Gate 3's remaining events (pushed_to_etsy, images_attached,
file_attached, edited, push_failed, publish_failed) are folded in the
push/Gate 3 slice when those events start being emitted.
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
            # A --force rebuild re-emits this event for an existing draft_id
            # (design §3: only state=published blocks a force rebuild, not
            # pushed) -- ON CONFLICT refreshes only the "created" fields and
            # never resets etsy_listing_id/state/copy/price/images, or a
            # force rebuild of an already-pushed draft would look unpushed
            # again and get double-pushed.
            conn.execute(
                "INSERT INTO proj_listing_drafts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,NULL,NULL,'[]',NULL,NULL,NULL,NULL,'[]',NULL,'built',?,NULL) "
                "ON CONFLICT(user_id, draft_id) DO UPDATE SET "
                "landing_file_id=excluded.landing_file_id, photo_id=excluded.photo_id, "
                "set_key=excluded.set_key, provider=excluded.provider, format=excluded.format, "
                "sku_source=excluded.sku_source, listing_type=excluded.listing_type, "
                "config_hash=excluded.config_hash",
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

        elif e.type == "listingdraft.copy_generated":
            conn.execute(
                "UPDATE proj_listing_drafts SET title=?, tags_json=?, description=? "
                "WHERE user_id=? AND draft_id=?",
                (p["title"], json.dumps(p["tags"]), p["description"], e.user_id, p["draft_id"]),
            )

        elif e.type == "listingdraft.priced":
            conn.execute(
                "UPDATE proj_listing_drafts SET price=?, currency=?, margin_floor=? "
                "WHERE user_id=? AND draft_id=?",
                (p["price"], p["currency"], p["margin_floor"], e.user_id, p["draft_id"]),
            )

        elif e.type == "listingdraft.pushed_to_etsy":
            conn.execute(
                "UPDATE proj_listing_drafts SET etsy_listing_id=?, state='pushed' "
                "WHERE user_id=? AND draft_id=?",
                (str(p["etsy_listing_id"]), e.user_id, p["draft_id"]),
            )

        elif e.type == "listingdraft.push_failed":
            etsy_listing_id = p.get("etsy_listing_id")
            if etsy_listing_id is not None:
                conn.execute(
                    "UPDATE proj_listing_drafts SET etsy_listing_id=?, state='push_failed' "
                    "WHERE user_id=? AND draft_id=?",
                    (str(etsy_listing_id), e.user_id, p["draft_id"]),
                )
            else:
                conn.execute(
                    "UPDATE proj_listing_drafts SET state='push_failed' "
                    "WHERE user_id=? AND draft_id=?",
                    (e.user_id, p["draft_id"]),
                )

        elif e.type == "gate3.published":
            conn.execute(
                "UPDATE proj_listing_drafts SET state='published', published_at=? "
                "WHERE user_id=? AND draft_id=?",
                (p.get("published_at"), e.user_id, p["draft_id"]),
            )

    conn.commit()
