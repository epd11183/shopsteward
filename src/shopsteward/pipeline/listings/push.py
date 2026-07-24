"""Push stage (design §1 dataflow, §7 digital-listing mechanics): for each
fully-built, not-yet-pushed draft -- create_draft_listing (price is set here,
Etsy's real updateListing has no price field) -> upload images in rank order
-> upload the sellable file -> update_listing with copy (title/description/
tags). Fake adapter by default (offline); Live only when the caller has
already confirmed pipeline.live_gate.live_etsy_write_open() (PRD §13
decision 41).

Any adapter failure emits listingdraft.push_failed and moves on to the next
draft -- one failure never aborts the batch. Idempotent: eligibility is
`etsy_listing_id IS NULL`, so a draft whose create_draft_listing already
succeeded (etsy_listing_id recorded) but a later stage failed is left alone
-- retrying create would risk a duplicate Etsy listing. That partial-failure
case is Gate 3's retry queue (M5a slice 4): _push_one is composed from
_create_stage/_images_stage/_file_stage/_update_stage, which gate3.retry_push
reuses directly to resume from whichever stage last failed instead of
re-running the whole thing.
"""

import json
import os
import sqlite3
from pathlib import Path

from shopsteward.adapters.etsy.auth import EtsyTokenStore
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.etsy.live import LiveEtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyDraftSpec, EtsyListingUpdate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.images import sellable_file_bytes
from shopsteward.pipeline.listings.models import BuildReport, ListingConfig
from shopsteward.pipeline.listings.projections import rebuild_listings


def build_etsy_write_adapter(*, live: bool) -> EtsyWriteAdapter:
    """Single construction path for EtsyWriteAdapter instances (vision_factory
    precedent): FakeEtsyWriteAdapter unless the caller has already confirmed
    the write gate is open."""
    if not live:
        return FakeEtsyWriteAdapter()

    api_key = os.environ.get("ETSY_API_KEY")
    if not api_key:
        raise RuntimeError("ETSY_API_KEY is not set; live Etsy writes need it.")
    store = EtsyTokenStore()
    tokens = store.load()
    if tokens is None or tokens.shop_id is None:
        raise RuntimeError("No Etsy tokens/shop on disk; run `shopsteward etsy auth` first.")
    return LiveEtsyWriteAdapter(api_key=api_key, shop_id=tokens.shop_id, token_store=store)


def _eligible_drafts(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT draft_id, landing_file_id, title, tags_json, description, price, currency, "
        "images_json FROM proj_listing_drafts WHERE user_id=? AND title IS NOT NULL "
        "AND price IS NOT NULL AND etsy_listing_id IS NULL AND state != 'published' "
        "ORDER BY draft_id",
        (user_id,),
    ).fetchall()


def _landing_path(conn: sqlite3.Connection, user_id: int, landing_file_id: str) -> str:
    row = conn.execute(
        "SELECT path FROM proj_landing_files WHERE user_id=? AND file_id=?",
        (user_id, landing_file_id),
    ).fetchone()
    if row is None:
        # A built draft always points at a landing row that produced it --
        # a missing row here is a data-integrity bug, not a normal push
        # failure, so this is allowed to crash rather than emit push_failed.
        raise LookupError(f"landing file {landing_file_id!r} not found for draft push")
    return row["path"]


def _create_stage(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyWriteAdapter,
    cfg: ListingConfig,
    draft_id: str,
    row: sqlite3.Row,
    tags: list[str],
) -> int:
    spec = EtsyDraftSpec(
        quantity=cfg.pricing.digital_quantity,
        title=row["title"],
        description=row["description"] or "",
        price=row["price"],
        who_made=cfg.etsy.who_made,
        when_made=cfg.etsy.when_made,
        taxonomy_id=cfg.etsy.taxonomy_id,
        is_supply=cfg.etsy.is_supply,
        tags=tags,
        should_auto_renew=cfg.etsy.should_auto_renew,
    )
    ref = adapter.create_draft_listing(spec)
    listing_id = ref.listing_id
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.pushed_to_etsy",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": listing_id,
                "listing_type": "download",
                "quantity": cfg.pricing.digital_quantity,
                "state": "draft",
            },
        ),
    )
    return listing_id


def _images_stage(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyWriteAdapter,
    draft_id: str,
    listing_id: int,
    images_json: str | None,
    *,
    skip_ranks: set[int] = frozenset(),
) -> None:
    """Emits one listingdraft.images_attached event per image, immediately
    after that image's upload succeeds -- not one event for the whole batch
    at the end. A mid-loop failure would otherwise leave no record of which
    images already made it to Etsy, and a retry would re-upload every image
    (duplicates on the live draft). skip_ranks lets a retry pass in ranks a
    prior attempt already attached (gate3.retry_push derives this from prior
    images_attached events) so it only uploads what's still missing."""
    images = sorted(json.loads(images_json or "[]"), key=lambda i: i["rank"])
    for img in images:
        if img["rank"] in skip_ranks:
            continue
        image_bytes = Path(img["path"]).read_bytes()
        img_ref = adapter.upload_listing_image(listing_id, image_bytes, rank=img["rank"])
        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.images_attached",
                payload={
                    "draft_id": draft_id,
                    "etsy_listing_id": listing_id,
                    "images": [
                        {
                            "etsy_image_id": img_ref.listing_image_id,
                            "rank": img["rank"],
                            "intent": img["intent"],
                        }
                    ],
                },
            ),
        )


def _file_stage(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyWriteAdapter,
    draft_id: str,
    listing_id: int,
    landing_path: str,
    sellable_max_bytes: int,
) -> None:
    file_bytes, sellable = sellable_file_bytes(landing_path, sellable_max_bytes)
    file_ref = adapter.upload_listing_file(
        listing_id, file_bytes, name=Path(landing_path).name, rank=1
    )
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.file_attached",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": listing_id,
                "etsy_file_id": file_ref.listing_file_id,
                "source": sellable.source,
                "sha256": sellable.sha256,
            },
        ),
    )


def _update_stage(
    adapter: EtsyWriteAdapter, listing_id: int, row: sqlite3.Row, tags: list[str]
) -> None:
    # Price was already set at create_draft_listing time -- Etsy's real
    # updateListing has no price field.
    adapter.update_listing(
        listing_id,
        EtsyListingUpdate(title=row["title"], description=row["description"], tags=tags),
    )


def _emit_push_failed(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    listing_id: int | None,
    stage: str,
    exc: Exception,
) -> None:
    # OSError: a bad/missing local image or sellable file is a per-draft
    # failure too -- one broken path must not abort the batch.
    code = exc.status_code if isinstance(exc, EtsyWriteError) else 0
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.push_failed",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": listing_id,
                "stage": stage,
                "error": {"code": code, "message": str(exc)[:300]},
            },
        ),
    )


def _push_one(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyWriteAdapter,
    cfg: ListingConfig,
    row: sqlite3.Row,
) -> bool:
    draft_id = row["draft_id"]
    tags = json.loads(row["tags_json"] or "[]")
    landing_path = _landing_path(conn, user_id, row["landing_file_id"])

    listing_id: int | None = None
    stage = "create"
    try:
        listing_id = _create_stage(conn, user_id, adapter, cfg, draft_id, row, tags)

        stage = "image"
        _images_stage(conn, user_id, adapter, draft_id, listing_id, row["images_json"])

        stage = "file"
        _file_stage(
            conn, user_id, adapter, draft_id, listing_id, landing_path, cfg.etsy.sellable_max_bytes
        )

        stage = "update"
        _update_stage(adapter, listing_id, row, tags)
        return True
    except (EtsyWriteError, OSError) as exc:
        _emit_push_failed(conn, user_id, draft_id, listing_id, stage, exc)
        return False


def push_drafts(
    conn: sqlite3.Connection, user_id: int, cfg: ListingConfig, adapter: EtsyWriteAdapter
) -> BuildReport:
    """Push every fully-built, not-yet-pushed draft. A failure on one draft
    is isolated -- it never aborts the batch."""
    rebuild_listings(conn)
    report = BuildReport()
    for row in _eligible_drafts(conn, user_id):
        if _push_one(conn, user_id, adapter, cfg, row):
            report.pushed += 1
        else:
            report.push_failed += 1
    rebuild_listings(conn)
    return report
