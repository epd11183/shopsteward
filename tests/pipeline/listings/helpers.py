"""Shared fixture builders for listings tests -- plain importable helpers,
not a conftest.py fixture module (mockups/helpers.py precedent)."""

import hashlib
import sqlite3
from pathlib import Path

from PIL import Image

from shopsteward.core.events import Event, append
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1


def seed_landing_file_with_mockup_set(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    photo_id: str,
    path: str,
    set_key: str,
    intents: list[str],
    mockups_dir: Path,
    user_id: int = USER_ID,
) -> None:
    """Appends a valid landing.file_observed + a completed mockup set (one
    mockup.generated per intent, in the given order, then
    mockupset.completed) and rebuilds the pipeline/mockups projections the
    listings build stage reads via raw SQL. Writes a real tiny JPEG at each
    mockup path (under mockups_dir) -- the push stage (M5a slice 3) reads
    these bytes off disk, so a placeholder path is no longer enough."""
    append(
        conn,
        Event(
            user_id=user_id,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": path,
                "base_name": None,
                "format": "JPEG",
                "width": 4000,
                "height": 3000,
                "color_space": "sRGB",
                "photo_id": photo_id,
            },
        ),
    )
    for i, intent in enumerate(intents):
        mockup_path = Path(mockups_dir) / photo_id / f"{intent}_{i}.jpg"
        mockup_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (50, 50), (i, i, i)).save(mockup_path, "JPEG")
        append(
            conn,
            Event(
                user_id=user_id,
                type="mockup.generated",
                payload={
                    "photo_id": photo_id,
                    "landing_file_id": file_id,
                    "set_key": set_key,
                    "intent": intent,
                    "template_id": None,
                    "path": str(mockup_path),
                    "params": {},
                },
            ),
        )
    append(
        conn,
        Event(
            user_id=user_id,
            type="mockupset.completed",
            payload={
                "photo_id": photo_id,
                "landing_file_id": file_id,
                "set_key": set_key,
                "count": len(intents),
                "config_hash": "mockup-cfg-hash",
                "template_library_hash": "template-lib-hash",
            },
        ),
    )
    rebuild_pipeline(conn)
    rebuild_mockups(conn)


def seed_fully_built_draft(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    file_id: str,
    photo_id: str,
    title: str,
    set_key: str,
    user_id: int = USER_ID,
    intents: list[str] = ["single"],  # noqa: B006 - never mutated
    format: str = "digital_download",  # noqa: A002 - matches the listingdraft.created field name
) -> str:
    """Appends the four upstream events push_drafts() expects (created,
    images_selected, copy_generated, priced) directly -- the same shape
    build_drafts() would have emitted -- without ever letting build_drafts's
    own auto-push run. Used by push.py and gate3.py tests that need to
    control which adapter/failure push_drafts() sees. One image per intent,
    ranked in the given order (default: a single "single"-intent image)."""
    photo_path = tmp_path / f"{photo_id}.jpg"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(photo_path, "JPEG")
    append(
        conn,
        Event(
            user_id=user_id,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": str(photo_path),
                "base_name": None,
                "format": "JPEG",
                "width": 100,
                "height": 100,
                "color_space": "sRGB",
                "photo_id": photo_id,
            },
        ),
    )
    rebuild_pipeline(conn)

    listing_config.seed(conn, user_id)
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, user_id)
    cfg_hash = listing_config.config_hash(cfg)
    draft_id = hashlib.sha256(f"{file_id}|{cfg_hash}|{set_key}".encode()).hexdigest()

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.created",
            payload={
                "draft_id": draft_id,
                "landing_file_id": file_id,
                "photo_id": photo_id,
                "set_key": set_key,
                "provider": "etsy_digital",
                "format": format,
                "sku_source": "etsy",
                "listing_type": "download",
                "config_hash": cfg_hash,
            },
        ),
    )
    images = []
    for rank, intent in enumerate(intents, start=1):
        mockup_path = tmp_path / "mockups" / photo_id / f"{intent}.jpg"
        mockup_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (50, 50), (4, 5, 6)).save(mockup_path, "JPEG")
        images.append({"path": str(mockup_path), "intent": intent, "rank": rank})
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.images_selected",
            payload={
                "draft_id": draft_id,
                "images": images,
                "sellable_file": {
                    "source": "landing_original",
                    "sha256": hashlib.sha256(photo_path.read_bytes()).hexdigest(),
                    "bytes": photo_path.stat().st_size,
                },
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.copy_generated",
            payload={
                "draft_id": draft_id,
                "title": title,
                "tags": ["wall art"],
                "description": "A digital download.",
                "materials": None,
                "model": "fixture",
                "provider": "fixture",
                "disclosure_appended": False,
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.priced",
            payload={
                "draft_id": draft_id,
                "format": "digital_download",
                "base_price": 12.00,
                "margin_floor": 6.00,
                "price": 12.00,
                "currency": "USD",
                "auto": True,
            },
        ),
    )
    rebuild_listings(conn)
    return draft_id
