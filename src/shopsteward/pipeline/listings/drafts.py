"""Draft-build stage: walk eligible landing files + their completed M4 mockup
set, emit listingdraft.created + .images_selected + .copy_generated +
.priced. Idempotent by draft_id = sha256(landing_file_id | config_hash |
set_key).

Reads proj_mockup_sets/proj_mockups (owned by the mockups module) via raw
SQL only -- listings must not import shopsteward.mockups (import-linter:
"mockups is imported by no lower layer" forbids pipeline -> mockups).

Reconciled skip predicate (slice 1 reviewer finding, design §3 amended):
a landing file with no existing draft gets a full build (created, images,
copy, price). A draft that already exists but is missing copy and/or price
events gets exactly those stages filled in on rerun (fill-forward) -- the
existing created/images_selected events are never re-emitted. A draft that
already carries both copy and price ("fully built") is skipped unless
--force. --force always re-runs the full build (copy/price/images; never a
second push). A published draft is never rebuilt, force or not.
"""

import hashlib
import json
import sqlite3

from shopsteward.core.events import Event, append
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.copy import build_copy_adapter, generate_copy
from shopsteward.pipeline.listings.images import order_listing_images, resolve_sellable_file
from shopsteward.pipeline.listings.models import BuildReport, ListingConfig, ListingImage
from shopsteward.pipeline.listings.pricing import apply_price, enforce_floor
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


def _existing_draft(conn: sqlite3.Connection, user_id: int, draft_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT landing_file_id, photo_id, images_json, title, price, state "
        "FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (user_id, draft_id),
    ).fetchone()


def _price_draft(
    conn: sqlite3.Connection, user_id: int, draft_id: str, format_: str, cfg: ListingConfig
) -> None:
    rules = cfg.pricing
    price = apply_price(format_, rules)
    enforce_floor(price, format_, rules)
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.priced",
            payload={
                "draft_id": draft_id,
                "format": format_,
                "base_price": price,
                "margin_floor": rules.formats[format_].margin_floor,
                "price": price,
                "currency": rules.currency,
                "auto": True,
            },
        ),
    )


def build_drafts(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    photo_id: str | None = None,
    force: bool = False,
    live_copy: bool = False,
) -> BuildReport:
    listing_config.seed(conn, user_id)
    tuning.seed(conn, user_id, TUNING_PROFILE_PATH)
    rebuild_pipeline(conn)
    rebuild_listings(conn)

    cfg = listing_config.get_config(conn, user_id)
    cfg_hash = listing_config.config_hash(cfg)
    profile = tuning.get_profile(conn, user_id)
    soft_cap_usd = profile.vision.monthly_soft_cap_usd
    copy_adapter = build_copy_adapter(cfg, live=live_copy)

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
        existing = _existing_draft(conn, user_id, draft_id)

        if existing is not None and existing["state"] == "published":
            result.skipped_idempotent += 1
            continue

        if existing is not None and not force:
            fully_built = existing["title"] is not None and existing["price"] is not None
            if fully_built:
                result.skipped_idempotent += 1
                continue

            # Fill-forward: keep the existing created/images_selected events,
            # only run whichever of copy/price is still missing.
            images = [ListingImage(**img) for img in json.loads(existing["images_json"] or "[]")]
            if existing["title"] is None:
                ran = generate_copy(
                    conn,
                    user_id,
                    draft_id,
                    existing["landing_file_id"],
                    existing["photo_id"],
                    images,
                    copy_adapter,
                    cfg,
                    live=live_copy,
                    soft_cap_usd=soft_cap_usd,
                )
                if ran:
                    result.copy_calls += 1
            if existing["price"] is None:
                _price_draft(conn, user_id, draft_id, "digital_download", cfg)
            result.drafts_built += 1
            continue

        # New draft, or --force rebuild of an existing non-published one.
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

        ran = generate_copy(
            conn,
            user_id,
            draft_id,
            landing_file_id,
            row["photo_id"],
            images,
            copy_adapter,
            cfg,
            live=live_copy,
            soft_cap_usd=soft_cap_usd,
        )
        if ran:
            result.copy_calls += 1
        _price_draft(conn, user_id, draft_id, "digital_download", cfg)

        result.drafts_built += 1

    rebuild_listings(conn)
    return result
