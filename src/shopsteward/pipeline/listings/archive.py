"""The managed local archive (source-asset head, design
2026-08-11-source-asset-head, slice 2): copies the UNTOUCHED original print
master bytes verbatim into `{asset_store.root}/{photo_id}/{sha256}{ext}` and
records `asset.archived`, so a future reprint can recover the master after
the landing folder is cleared. No resize, no re-encode, nothing generative
(CLAUDE.md: AI never touches the photograph) -- the deterministic sellable
re-encode stays downstream, at build time, from this archived original.

Idempotent: if `proj_asset_store` already has this (photo_id, format) with
the SAME sha256, this is a no-op -- no dup file, no dup event. Config-gated:
a no-op entirely when `asset_store.enabled` is False. Purely additive: does
not touch the digital/POD draft rows this module's callers also write.
"""

import hashlib
import shutil
import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings.models import AssetStoreConfig


def archive_master(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: AssetStoreConfig,
    *,
    photo_id: str,
    source_landing_file_id: str,
    path: str,
    format: str,
    width: int | None,
    height: int | None,
) -> bool:
    """Returns True if a new copy+event was recorded, False if disabled or
    already archived (same photo_id/format/sha256)."""
    if not cfg.enabled:
        return False

    raw = Path(path).read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()

    # Read the event log directly, not proj_asset_store -- build_pod_drafts
    # calls this once per product-type draft within ONE rebuild_listings()
    # window, so the projection is stale between calls; the event log is
    # always live.
    # ponytail: full-scan idempotency over asset.archived; add an
    # index/projection check if the archive count grows.
    already_archived = any(
        e.user_id == user_id
        and e.payload["photo_id"] == photo_id
        and e.payload["format"] == format
        and e.payload["sha256"] == sha256
        for e in read_all(conn, "asset.archived")
    )
    if already_archived:
        return False

    # Relative key -- never an absolute path or credential in the event
    # payload (design decision 48 precedent). The original file extension is
    # kept verbatim rather than guessed from `format`.
    ext = Path(path).suffix
    stored_key = f"{photo_id}/{sha256}{ext}"
    dest = asset_store_config.resolve_root(cfg) / stored_key
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copyfile(path, dest)

    append(
        conn,
        Event(
            user_id=user_id,
            type="asset.archived",
            payload={
                "photo_id": photo_id,
                "sha256": sha256,
                "bytes": len(raw),
                "width": width,
                "height": height,
                "format": format,
                "stored_key": stored_key,
                "source_landing_file_id": source_landing_file_id,
            },
        ),
    )
    return True
