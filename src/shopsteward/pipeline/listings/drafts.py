"""Draft-build stage (slice 1 of M5a): walk eligible landing files + their
completed M4 mockup set, emit listingdraft.created + .images_selected.
Idempotent by draft_id = sha256(landing_file_id | config_hash | set_key).

Reads proj_mockup_sets/proj_mockups (owned by the mockups module) via raw
SQL only -- listings must not import shopsteward.mockups (import-linter:
"mockups is imported by no lower layer" forbids pipeline -> mockups).
Copy generation, pricing, and the Etsy push are later M5a slices; this stage
leaves title/description/price/etsy_listing_id NULL and state='built'.
"""

import hashlib
import sqlite3

from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.images import order_listing_images, resolve_sellable_file
from shopsteward.pipeline.listings.models import BuildReport
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline


def _eligible_landing_rows(
    conn: sqlite3.Connection, user_id: int, photo_id: str | None
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT file_id, path, photo_id FROM proj_landing_files "
        "WHERE user_id=? AND status='valid' ORDER BY file_id",
        (user_id,),
    ).fetchall()
    if photo_id is None:
        return rows
    return [
        row
        for row in rows
        if row["photo_id"] == photo_id
        or (row["photo_id"] is None and f"file-{row['file_id'][:12]}" == photo_id)
    ]


def _mockup_projections_ready(conn: sqlite3.Connection) -> bool:
    # proj_mockup_sets/proj_mockups are owned by the mockups module and only
    # exist once `shopsteward mockups run` has rebuilt its projections at
    # least once; treat "table absent" the same as "no mockup set yet"
    # rather than crashing the build.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proj_mockup_sets'"
    ).fetchone()
    return row is not None


def _completed_mockup_set(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str
) -> sqlite3.Row | None:
    # ponytail: a landing file can in principle have >1 completed set (a
    # mockup config/template-library change reruns without --force replacing
    # the old set_key); most-recent-by-rowid is the practical "current" one.
    # Upgrade to an explicit "active set" pointer if that ever matters.
    return conn.execute(
        "SELECT set_key FROM proj_mockup_sets WHERE user_id=? AND landing_file_id=? "
        "ORDER BY rowid DESC LIMIT 1",
        (user_id, landing_file_id),
    ).fetchone()


def _mockup_rows(conn: sqlite3.Connection, user_id: int, set_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT path, intent FROM proj_mockups WHERE user_id=? AND set_key=? ORDER BY path",
        (user_id, set_key),
    ).fetchall()
    return [dict(row) for row in rows]


def build_drafts(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    photo_id: str | None = None,
    force: bool = False,
) -> BuildReport:
    listing_config.seed(conn, user_id)
    rebuild_pipeline(conn)
    rebuild_listings(conn)

    cfg = listing_config.get_config(conn, user_id)
    cfg_hash = listing_config.config_hash(cfg)

    result = BuildReport()
    if not _mockup_projections_ready(conn):
        return result

    for row in _eligible_landing_rows(conn, user_id, photo_id):
        landing_file_id = row["file_id"]
        mockup_set = _completed_mockup_set(conn, user_id, landing_file_id)
        if mockup_set is None:
            continue
        set_key = mockup_set["set_key"]

        draft_id = hashlib.sha256(f"{landing_file_id}|{cfg_hash}|{set_key}".encode()).hexdigest()

        existing = conn.execute(
            "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
            (user_id, draft_id),
        ).fetchone()
        if existing and not force:
            result.skipped_idempotent += 1
            continue

        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.created",
                payload={
                    "draft_id": draft_id,
                    "landing_file_id": landing_file_id,
                    "photo_id": row["photo_id"],
                    "set_key": set_key,
                    "provider": "etsy_digital",
                    "format": "digital_download",
                    "sku_source": "etsy",
                    "listing_type": "download",
                    "config_hash": cfg_hash,
                },
            ),
        )

        mockups = _mockup_rows(conn, user_id, set_key)
        images = order_listing_images(mockups, cfg)
        sellable_file = resolve_sellable_file(row["path"], cfg.etsy.sellable_max_bytes)

        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.images_selected",
                payload={
                    "draft_id": draft_id,
                    "images": [img.model_dump() for img in images],
                    "sellable_file": sellable_file.model_dump(),
                },
            ),
        )

        result.drafts_built += 1

    rebuild_listings(conn)
    return result
