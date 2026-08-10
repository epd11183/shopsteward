"""POD enrichment (Phase C, slice 4, design §7.1/§3): for every POD draft
that reached `listingdraft.provider_linked` (pod/provider.py, slice 3) and
has not yet been enriched, generate copy (reusing the Phase-A vision signals,
same `generate_copy` the digital path calls) and push copy + images onto the
ALREADY-EXISTING Etsy draft (created by Gelato, not by this codebase --
CLAUDE.md's "POD-first listing creation": the provider creates the product
and pushes the Etsy draft, we only enrich it). Never touches price --
Etsy's real updateListing has no price field, and the POD price was already
set at Gelato create time (pod/provider.py's PodProductSpec).

Mirrors push.py's staging/idempotency shape: `_images_stage`/`_update_stage`
are reused directly from push.py (gate3.retry_push precedent) rather than
duplicated. There is no `_create_stage` here -- the Etsy listing already
exists -- and no `_file_stage` -- POD has no digital sellable file.

Image source (confirmed by reading pod/build.py + mockups/projections.py,
per the task's DISCOVERY-FIRST instruction): pod/build.py's own docstring
says "POD images are an enrichment-stage (slice 4) concern, not a build-stage
one" and never emits listingdraft.images_selected, so a POD draft's
images_json is '[]' until this module fills it in. Every POD draft carries
the SAME landing_file_id a digital draft for that photo would carry (both
key off proj_landing_files), and the M4 mockup module keys its completed
sets by landing_file_id too (proj_mockup_sets/proj_mockups) -- so this reuses
exactly the digital build stage's own lookup (drafts.py's
_completed_mockup_set/_mockup_rows) and `order_listing_images` (images.py)
to select the same mockup set the digital draft for that photo would use. If
no completed mockup set exists yet for a draft's landing_file_id, that draft
is skipped this pass (not failed) -- mirrors build_drafts's own "mockup set
not ready yet, try later" skip, not a data-integrity error.

`listingdraft.images_selected` is reused for the images plan (title/tags/
description already reuse `listingdraft.copy_generated` unmodified) rather
than inventing a new event type: its fold only required `sellable_file`
because every prior caller was digital (which always has one); POD has no
sellable file, so the fold now tolerates a payload with no `sellable_file`
key at all (projections.py change, kept backward compatible).
"""

import json
import sqlite3

from pydantic import BaseModel

from shopsteward.adapters.copy.interface import CopyAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.listings.copy import generate_copy
from shopsteward.pipeline.listings.drafts import (
    _completed_mockup_set,
    _mockup_projections_ready,
    _mockup_rows,
)
from shopsteward.pipeline.listings.images import order_listing_images
from shopsteward.pipeline.listings.models import ListingConfig, ListingImage
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.push import _images_stage, _update_stage

# A POD draft's pod_status cycles NULL -> "publishing" -> "linked"/"failed"
# (pod/provider.py). This module adds two more terminal values on the SAME
# column (that column is "the POD lifecycle state", not exclusively
# provider.py's) -- "enriched" is the final success state _eligible_drafts
# excludes, "enrich_failed" is retried on the next call (unlike
# provider.py's own "failed", which link_pod_drafts never retries without
# --force -- an enrichment failure is expected to be transient: a bad image
# path or a momentary Etsy write error, not a bad catalog entry).
_ELIGIBLE_POD_STATUSES = ("linked", "enrich_failed")


class PodEnrichReport(BaseModel):
    """`pod enrich`'s report (PodBuildReport/PodLinkReport, twin)."""

    enriched: int = 0
    skipped: int = 0
    failed: int = 0


def _eligible_drafts(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT draft_id, landing_file_id, photo_id, etsy_listing_id, images_json, "
        "title, tags_json, description FROM proj_listing_drafts WHERE user_id=? "
        "AND pod_config_hash IS NOT NULL AND pod_status IN (?,?) ORDER BY draft_id",
        (user_id, *_ELIGIBLE_POD_STATUSES),
    ).fetchall()


def _fetch_row(conn: sqlite3.Connection, user_id: int, draft_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT draft_id, landing_file_id, photo_id, etsy_listing_id, images_json, "
        "title, tags_json, description FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (user_id, draft_id),
    ).fetchone()
    if row is None:  # pragma: no cover - defensive, caller just wrote this row
        raise RuntimeError(f"pod enrich row for {draft_id!r} disappeared")
    return row


def _planned_images(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str, cfg: ListingConfig
) -> list[ListingImage] | None:
    """None means "not ready yet" (no completed mockup set for this landing
    file), distinct from an empty list (a completed set that yielded zero
    orderable images) -- the caller skips-and-retries on None only."""
    if not _mockup_projections_ready(conn):
        return None
    mockup_set = _completed_mockup_set(conn, user_id, landing_file_id)
    if mockup_set is None:
        return None
    mockups = _mockup_rows(conn, user_id, mockup_set["set_key"])
    return order_listing_images(mockups, cfg)


def _already_attached_ranks(conn: sqlite3.Connection, user_id: int, draft_id: str) -> set[int]:
    # Mirrors gate3.retry_push: _images_stage emits one event per image, so
    # this accumulates across however many prior enrich attempts happened.
    return {
        img["rank"]
        for e in read_all(conn, "listingdraft.images_attached")
        if e.user_id == user_id and e.payload.get("draft_id") == draft_id
        for img in e.payload.get("images", [])
    }


def _emit_enrich_failed(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    listing_id: int,
    stage: str,
    exc: Exception,
) -> None:
    code = exc.status_code if isinstance(exc, EtsyWriteError) else 0
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.enrich_failed",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": listing_id,
                "stage": stage,
                "error": {"code": code, "message": str(exc)[:300]},
            },
        ),
    )


def _enrich_one(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyWriteAdapter,
    copy_adapter: CopyAdapter,
    cfg: ListingConfig,
    row: sqlite3.Row,
    *,
    live: bool,
    soft_cap_usd: float,
) -> str:
    """Returns "enriched" | "skipped" (not ready yet / soft cap -- retried
    next run) | "failed" (adapter error, retried next run via
    pod_status='enrich_failed')."""
    draft_id = row["draft_id"]
    landing_file_id = row["landing_file_id"]
    listing_id = int(row["etsy_listing_id"])

    images_json = row["images_json"]
    if images_json in (None, "[]"):
        planned = _planned_images(conn, user_id, landing_file_id, cfg)
        if planned is None:
            return "skipped"
        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.images_selected",
                payload={
                    "draft_id": draft_id,
                    "images": [img.model_dump() for img in planned],
                },
            ),
        )
        images_json = json.dumps([img.model_dump() for img in planned])
        images = planned
    else:
        images = [ListingImage(**img) for img in json.loads(images_json)]

    if row["title"] is None:
        effective_photo_id = row["photo_id"] or f"file-{landing_file_id[:12]}"
        ran = generate_copy(
            conn,
            user_id,
            draft_id,
            landing_file_id,
            effective_photo_id,
            images,
            copy_adapter,
            cfg,
            live=live,
            soft_cap_usd=soft_cap_usd,
        )
        if not ran:
            return "skipped"
        # generate_copy only appends the event -- proj_listing_drafts.title
        # stays NULL until the projection is rebuilt, and _update_stage below
        # needs the fresh title/description.
        rebuild_listings(conn)
        row = _fetch_row(conn, user_id, draft_id)

    tags = json.loads(row["tags_json"] or "[]")
    skip_ranks = _already_attached_ranks(conn, user_id, draft_id)
    stage = "image"
    try:
        _images_stage(
            conn, user_id, adapter, draft_id, listing_id, images_json, skip_ranks=skip_ranks
        )
        stage = "update"
        _update_stage(adapter, listing_id, row, tags)
    except (EtsyWriteError, OSError) as exc:
        _emit_enrich_failed(conn, user_id, draft_id, listing_id, stage, exc)
        return "failed"

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.enriched",
            payload={"draft_id": draft_id, "etsy_listing_id": listing_id},
        ),
    )
    return "enriched"


def enrich_pod_drafts(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    etsy_adapter: EtsyWriteAdapter,
    copy_adapter: CopyAdapter,
    cfg: ListingConfig,
    live: bool = False,
    soft_cap_usd: float | None = None,
) -> PodEnrichReport:
    """cfg is the DIGITAL listing config (listing.json) -- copy generation
    and image ordering are the exact same machinery build_drafts uses for
    digital drafts (see module docstring); pod.json carries no image_order/
    image_cap of its own. Callers seed/rebuild listing config the same way
    build_drafts does; this only rebuilds the listings projection."""
    rebuild_listings(conn)

    if soft_cap_usd is None:
        tuning.seed(conn, user_id, TUNING_PROFILE_PATH)
        profile = tuning.get_profile(conn, user_id)
        soft_cap_usd = profile.vision.monthly_soft_cap_usd

    report = PodEnrichReport()
    for row in _eligible_drafts(conn, user_id):
        outcome = _enrich_one(
            conn,
            user_id,
            etsy_adapter,
            copy_adapter,
            cfg,
            row,
            live=live,
            soft_cap_usd=soft_cap_usd,
        )
        if outcome == "enriched":
            report.enriched += 1
        elif outcome == "skipped":
            report.skipped += 1
        else:
            report.failed += 1

    rebuild_listings(conn)
    return report
