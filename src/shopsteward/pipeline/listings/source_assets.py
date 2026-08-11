"""The linkage resolver (source-asset head, design
2026-08-11-source-asset-head, slice 1): given an Etsy listing_id, look up
which photograph produced it and whether its source bytes are still
recoverable (on disk, archived, or neither). Pure read -- no writes, no
network. This is what a future gap-fill capability calls to decide whether a
best-seller is reprintable; it does NOT build a reprint itself (that is a
later slice, out of scope here).

No new linkage table: `proj_listing_drafts` already carries
photo_id/landing_file_id/etsy_listing_id on one row. A listing_id with no
matching draft row is a manual/pre-pipeline listing -- not reprintable in
this MVP -- and resolves to None so the caller can surface that honestly.
"""

import os
import sqlite3

from pydantic import BaseModel


class SourceRef(BaseModel):
    photo_id: str | None
    landing_file_id: str | None
    on_disk_path: str | None
    on_disk_present: bool
    archived: bool


def resolve_source(
    conn: sqlite3.Connection, user_id: int, etsy_listing_id: int
) -> SourceRef | None:
    """`proj_listing_drafts.etsy_listing_id` is TEXT (shared with the
    digital push path, which stores the provider id as a string) while a
    sale's listing_id is an INTEGER -- CAST bridges the type seam."""
    draft = conn.execute(
        "SELECT photo_id, landing_file_id FROM proj_listing_drafts "
        "WHERE user_id=? AND etsy_listing_id IS NOT NULL AND CAST(etsy_listing_id AS INTEGER)=?",
        (user_id, etsy_listing_id),
    ).fetchone()
    if draft is None:
        return None

    on_disk_path = None
    if draft["landing_file_id"] is not None:
        landing = conn.execute(
            "SELECT path FROM proj_landing_files WHERE user_id=? AND file_id=?",
            (user_id, draft["landing_file_id"]),
        ).fetchone()
        on_disk_path = landing["path"] if landing is not None else None

    archived = False
    if draft["photo_id"] is not None:
        archived = (
            conn.execute(
                "SELECT 1 FROM proj_asset_store WHERE user_id=? AND photo_id=? LIMIT 1",
                (user_id, draft["photo_id"]),
            ).fetchone()
            is not None
        )

    return SourceRef(
        photo_id=draft["photo_id"],
        landing_file_id=draft["landing_file_id"],
        on_disk_path=on_disk_path,
        on_disk_present=on_disk_path is not None and os.path.exists(on_disk_path),
        archived=archived,
    )
