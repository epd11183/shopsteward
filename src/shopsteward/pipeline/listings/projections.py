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

M5b note: pod/projections.py's rebuild_pod_config() is a SEPARATE
entrypoint, not called from here and not calling here. It folds a
different event namespace (podconfig.*) into a different table
(proj_pod_config) and has no dependency on proj_listing_config/
proj_listing_drafts. A pod-aware caller (pod/build.py, slice 2+) must
invoke rebuild_listings() and rebuild_pod_config() explicitly, side by
side -- there is no combined entrypoint.

M5b slice 2 (design §3) extends proj_listing_drafts IN PLACE rather than
adding a pod-specific table: pod_config_hash, provider_product_id,
pod_status, variants_json, unit_cost, print_file_sha256, print_file_key.
POD drafts reuse the SAME listingdraft.* namespace (decision 37/44) --
listingdraft.created carries pod_config_hash only when it's a POD draft
(NULL for digital); listingdraft.priced is reused+extended: a payload
carrying "variants" is the POD shape (unit_cost/variants_json), anything
else is M5a's digital shape (price/margin_floor), same event type, two
payload shapes distinguished by key presence -- exactly like this file's
existing "reused, extended" precedent for other event types. variants_json
is fold-MERGED by `format` across .variants_selected -> .priced (mirrors
images_attached's merge-by-rank), so a variant's aspect/dpi (selection
stage) and its base_cost/retail_price/net/margin_pct (pricing stage) end up
in the same row instead of the second event's write clobbering the first's.
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
    pod_config_hash TEXT, provider_product_id TEXT, pod_status TEXT,
    variants_json TEXT NOT NULL DEFAULT '[]', unit_cost REAL,
    print_file_sha256 TEXT, print_file_key TEXT,
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
            # again and get double-pushed. pod_config_hash is NULL for a
            # digital draft (M5a never sets it) and the POD config's hash
            # for a physical one (M5b, design §3).
            conn.execute(
                "INSERT INTO proj_listing_drafts VALUES "
                "(?,?,?,?,?,?,?,?,?,?,NULL,NULL,'[]',NULL,NULL,NULL,NULL,'[]',NULL,'built',?,NULL,"
                "?,NULL,NULL,'[]',NULL,NULL,NULL) "
                "ON CONFLICT(user_id, draft_id) DO UPDATE SET "
                "landing_file_id=excluded.landing_file_id, photo_id=excluded.photo_id, "
                "set_key=excluded.set_key, provider=excluded.provider, format=excluded.format, "
                "sku_source=excluded.sku_source, listing_type=excluded.listing_type, "
                "config_hash=excluded.config_hash, pod_config_hash=excluded.pod_config_hash",
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
                    p.get("pod_config_hash"),
                ),
            )

        elif e.type == "listingdraft.variants_selected":
            conn.execute(
                "UPDATE proj_listing_drafts SET variants_json=? WHERE user_id=? AND draft_id=?",
                (json.dumps(p["variants"]), e.user_id, p["draft_id"]),
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
            if "variants" in p:
                # POD shape (design §3): merge each priced variant's extra
                # fields (base_cost/shipping_est/retail_price/net/margin_pct)
                # into the SAME by-format dict variants_selected already
                # wrote (aspect/size/variant_key/dpi) -- images_attached's
                # merge-by-rank precedent -- instead of clobbering it.
                row = conn.execute(
                    "SELECT variants_json FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
                    (e.user_id, p["draft_id"]),
                ).fetchone()
                by_format = (
                    {v["format"]: v for v in json.loads(row["variants_json"] or "[]")}
                    if row is not None
                    else {}
                )
                for v in p["variants"]:
                    by_format.setdefault(v["format"], {"format": v["format"]}).update(v)
                conn.execute(
                    "UPDATE proj_listing_drafts SET unit_cost=?, variants_json=?, currency=? "
                    "WHERE user_id=? AND draft_id=?",
                    (
                        p["unit_cost"],
                        json.dumps(list(by_format.values())),
                        p["currency"],
                        e.user_id,
                        p["draft_id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE proj_listing_drafts SET price=?, currency=?, margin_floor=? "
                    "WHERE user_id=? AND draft_id=?",
                    (p["price"], p["currency"], p["margin_floor"], e.user_id, p["draft_id"]),
                )

        elif e.type == "listingdraft.print_file_prepared":
            conn.execute(
                "UPDATE proj_listing_drafts SET print_file_sha256=? WHERE user_id=? AND draft_id=?",
                (p["sha256"], e.user_id, p["draft_id"]),
            )

        elif e.type == "listingdraft.print_file_hosted":
            conn.execute(
                "UPDATE proj_listing_drafts SET print_file_key=?, print_file_sha256=? "
                "WHERE user_id=? AND draft_id=?",
                (p["file_key"], p["sha256"], e.user_id, p["draft_id"]),
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
