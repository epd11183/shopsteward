"""Derived read models for the listings module: proj_listing_config,
proj_listing_drafts. Drop-and-rebuild, own schema, own rebuild entrypoint
(rebuild_listings), mirroring mockups/projections.py.

Ownership rule: listings never writes proj_photos, proj_landing_files, or
proj_mockups (owned by editing/pipeline/mockups respectively).

Slice 1 folded listingconfig.seeded/.updated + listingdraft.created +
.images_selected. Slice 2 adds listingdraft.copy_generated (title, tags_json,
description) + listingdraft.priced (price, currency, margin_floor) + a
minimal gate3.published -> state='published' fold (the reconciled skip
predicate, design §3, needs a real published state to check against). Slice 3
adds pushed_to_etsy/file_attached (file source already lands via
.images_selected, so this event needs no additional columns)/push_failed.
Slice 4 (Gate 3) adds listingdraft.edited (partial title/tags/description/
price refresh), listingdraft.push_resumed -> state='pushed' (a Gate 3 retry
that resumed from the image/file/update stage never re-emits
pushed_to_etsy, so this is the only event that clears a stuck push_failed
state after success), and gate3.publish_failed -> state='publish_failed'.
gate3.approved is not folded -- it never changes projected state (Gate 3
still shows the draft as "pushed" until publish succeeds or fails) and
carries no field the projection needs.

listingdraft.images_attached is emitted once PER image (reviewer fix-up,
M5a slice 4): the fold merges each event's image (by rank) into the
existing images_json instead of assuming one event covers the whole set,
so a resumed/retried push's later events don't clobber earlier ones.
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

        elif e.type == "listingdraft.images_attached":
            row = conn.execute(
                "SELECT images_json FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
                (e.user_id, p["draft_id"]),
            ).fetchone()
            if row is not None:
                by_rank = {img["rank"]: img for img in json.loads(row["images_json"] or "[]")}
                for attached in p.get("images", []):
                    if attached["rank"] in by_rank:
                        by_rank[attached["rank"]]["etsy_image_id"] = attached["etsy_image_id"]
                conn.execute(
                    "UPDATE proj_listing_drafts SET images_json=? WHERE user_id=? AND draft_id=?",
                    (json.dumps(list(by_rank.values())), e.user_id, p["draft_id"]),
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

        elif e.type == "listingdraft.edited":
            fields = p.get("fields") or {}
            sets: list[str] = []
            params: list = []
            if "title" in fields:
                sets.append("title=?")
                params.append(fields["title"])
            if "tags" in fields:
                sets.append("tags_json=?")
                params.append(json.dumps(fields["tags"]))
            if "description" in fields:
                sets.append("description=?")
                params.append(fields["description"])
            if p.get("price") is not None:
                sets.append("price=?")
                params.append(p["price"])
            if sets:
                params.extend([e.user_id, p["draft_id"]])
                conn.execute(
                    f"UPDATE proj_listing_drafts SET {', '.join(sets)} "
                    "WHERE user_id=? AND draft_id=?",
                    params,
                )

        elif e.type == "gate3.published":
            conn.execute(
                "UPDATE proj_listing_drafts SET state='published', published_at=? "
                "WHERE user_id=? AND draft_id=?",
                (p.get("published_at"), e.user_id, p["draft_id"]),
            )

        elif e.type == "listingdraft.push_resumed":
            conn.execute(
                "UPDATE proj_listing_drafts SET state='pushed' WHERE user_id=? AND draft_id=?",
                (e.user_id, p["draft_id"]),
            )

        elif e.type == "gate3.publish_failed":
            conn.execute(
                "UPDATE proj_listing_drafts SET state='publish_failed' "
                "WHERE user_id=? AND draft_id=?",
                (e.user_id, p["draft_id"]),
            )

    conn.commit()
