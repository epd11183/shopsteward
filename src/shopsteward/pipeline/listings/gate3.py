"""Gate 3 (design §8, PRD §13 decisions 40-41): default-accept publish queue.

queue() reads pushed/push_failed/publish_failed drafts as Gate3Card rows
(carrying computed economics + push/publish retry info). edit() applies a
partial title/tags/description/price change, re-validating price against the
format's margin floor and tags against Etsy's count/length limits, then
mirrors title/tags/description onto the Etsy draft via update_listing and a
changed price via update_listing_price (price has no field on
updateListing itself -- Etsy's real endpoint for a post-create price change
is updateListingInventory, PRD §13 decision 39/41). publish() is the SOLE
call site for
EtsyWriteAdapter.publish_listing anywhere in this codebase (PRD §13
decision 41) -- no other module may call it. retry_push() resumes a
push_failed draft from whichever stage last failed, reusing push.py's
resumable stage functions instead of re-running the whole push (which would
risk a duplicate Etsy listing for a draft that already has an
etsy_listing_id).
"""

import json
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.etsy.models import EtsyListingUpdate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.models import (
    Economics,
    Gate3Card,
    GateEditFields,
    ListingConfig,
    ListingImage,
)
from shopsteward.pipeline.listings.pricing import compute_economics, enforce_floor
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.push import (
    _create_stage,
    _emit_push_failed,
    _file_stage,
    _images_stage,
    _landing_path,
    _update_stage,
)

_QUEUE_STATES = ("pushed", "push_failed", "publish_failed")
_FORMAT = "digital_download"  # only digital format M5a builds (design §1)


def _last_error(
    conn: sqlite3.Connection, user_id: int, draft_id: str, event_type: str
) -> str | None:
    events = [
        e
        for e in read_all(conn, event_type)
        if e.user_id == user_id and e.payload.get("draft_id") == draft_id
    ]
    if not events:
        return None
    error = events[-1].payload.get("error") or {}
    return error.get("message")


def _row_to_card(
    conn: sqlite3.Connection, user_id: int, cfg: ListingConfig, row: sqlite3.Row
) -> Gate3Card:
    economics: Economics | None = None
    if row["price"] is not None:
        economics = compute_economics(row["price"], cfg.pricing)

    retry_error = None
    if row["state"] == "push_failed":
        retry_error = _last_error(conn, user_id, row["draft_id"], "listingdraft.push_failed")
    elif row["state"] == "publish_failed":
        retry_error = _last_error(conn, user_id, row["draft_id"], "gate3.publish_failed")

    return Gate3Card(
        draft_id=row["draft_id"],
        etsy_listing_id=row["etsy_listing_id"],
        title=row["title"],
        tags=json.loads(row["tags_json"] or "[]"),
        description=row["description"],
        price=row["price"],
        currency=row["currency"],
        margin_floor=row["margin_floor"],
        economics=economics,
        images=[ListingImage(**img) for img in json.loads(row["images_json"] or "[]")],
        file_source=row["file_source"],
        state=row["state"],
        retry_error=retry_error,
    )


def _fetch_row(conn: sqlite3.Connection, user_id: int, draft_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT draft_id, etsy_listing_id, title, tags_json, description, price, currency, "
        "margin_floor, images_json, file_source, landing_file_id, state "
        "FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (user_id, draft_id),
    ).fetchone()


def _card_for(
    conn: sqlite3.Connection, user_id: int, draft_id: str, cfg: ListingConfig
) -> Gate3Card:
    row = _fetch_row(conn, user_id, draft_id)
    if row is None:  # pragma: no cover - defensive, caller already validated draft_id exists
        raise RuntimeError(f"gate3 card for {draft_id!r} disappeared")
    return _row_to_card(conn, user_id, cfg, row)


def queue(conn: sqlite3.Connection, user_id: int) -> list[Gate3Card]:
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, user_id)
    rows = conn.execute(
        "SELECT draft_id, etsy_listing_id, title, tags_json, description, price, currency, "
        "margin_floor, images_json, file_source, landing_file_id, state "
        "FROM proj_listing_drafts WHERE user_id=? AND state IN (?,?,?) ORDER BY draft_id",
        (user_id, *_QUEUE_STATES),
    ).fetchall()
    return [_row_to_card(conn, user_id, cfg, row) for row in rows]


def edit(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    fields: dict,
    adapter: EtsyWriteAdapter,
) -> Gate3Card:
    rebuild_listings(conn)
    row = _fetch_row(conn, user_id, draft_id)
    if row is None:
        raise ValueError(f"unknown draft_id {draft_id!r}")
    if row["state"] == "published":
        raise ValueError(f"draft {draft_id!r} is already published and can no longer be edited")
    if row["etsy_listing_id"] is None:
        raise ValueError(f"draft {draft_id!r} has not been pushed to Etsy yet")

    try:
        edit_fields = GateEditFields.model_validate(fields)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    cfg = listing_config.get_config(conn, user_id)

    price: float | None = None
    if edit_fields.price is not None:
        price = edit_fields.price
        enforce_floor(price, _FORMAT, cfg.pricing)  # raises BelowFloor

    etsy_fields = edit_fields.model_dump(exclude_none=True, exclude={"price"})
    if not etsy_fields and price is None:
        raise ValueError("no fields to edit")

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.edited",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": row["etsy_listing_id"],
                "fields": etsy_fields,
                "price": price,
            },
        ),
    )

    listing_id = int(row["etsy_listing_id"])
    if etsy_fields:
        adapter.update_listing(listing_id, EtsyListingUpdate(**etsy_fields))
    if price is not None:
        adapter.update_listing_price(listing_id, price)

    rebuild_listings(conn)
    return _card_for(conn, user_id, draft_id, cfg)


def publish(
    conn: sqlite3.Connection, user_id: int, draft_id: str, adapter: EtsyWriteAdapter
) -> Gate3Card:
    """The sole call site for EtsyWriteAdapter.publish_listing anywhere in
    this codebase (PRD §13 decision 41). Never raises on an adapter failure
    -- emits gate3.publish_failed and returns the card in that state so the
    API layer can return a clean error response instead of a 500."""
    rebuild_listings(conn)
    row = _fetch_row(conn, user_id, draft_id)
    if row is None:
        raise ValueError(f"unknown draft_id {draft_id!r}")
    if row["state"] not in ("pushed", "publish_failed"):
        raise ValueError(f"draft {draft_id!r} is not publishable (state={row['state']!r})")
    if row["etsy_listing_id"] is None:
        raise ValueError(f"draft {draft_id!r} has no Etsy listing id yet")

    listing_id = int(row["etsy_listing_id"])
    cfg = listing_config.get_config(conn, user_id)

    append(
        conn,
        Event(
            user_id=user_id,
            type="gate3.approved",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": listing_id,
                "final_price": row["price"],
                "currency": row["currency"] or cfg.pricing.currency,
            },
        ),
    )

    try:
        adapter.publish_listing(listing_id)
    except EtsyWriteError as exc:
        append(
            conn,
            Event(
                user_id=user_id,
                type="gate3.publish_failed",
                payload={
                    "draft_id": draft_id,
                    "etsy_listing_id": listing_id,
                    "error": {"code": exc.status_code, "message": str(exc)[:300]},
                },
            ),
        )
        rebuild_listings(conn)
        return _card_for(conn, user_id, draft_id, cfg)

    append(
        conn,
        Event(
            user_id=user_id,
            type="gate3.published",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": listing_id,
                "state": "active",
                "published_at": datetime.now(UTC).isoformat(),
            },
        ),
    )
    rebuild_listings(conn)
    return _card_for(conn, user_id, draft_id, cfg)


def retry_push(
    conn: sqlite3.Connection, user_id: int, draft_id: str, adapter: EtsyWriteAdapter
) -> Gate3Card:
    """Resume a push_failed draft. A NULL etsy_listing_id means
    create_draft_listing never succeeded -- redo the full push. Otherwise
    resume from whichever stage the last listingdraft.push_failed event
    recorded (never re-run create, which would risk a duplicate Etsy
    listing)."""
    rebuild_listings(conn)
    row = _fetch_row(conn, user_id, draft_id)
    if row is None:
        raise ValueError(f"unknown draft_id {draft_id!r}")
    if row["state"] != "push_failed":
        raise ValueError(f"draft {draft_id!r} is not push_failed (state={row['state']!r})")

    cfg = listing_config.get_config(conn, user_id)
    tags = json.loads(row["tags_json"] or "[]")
    landing_path = _landing_path(conn, user_id, row["landing_file_id"])
    listing_id: int | None = (
        int(row["etsy_listing_id"]) if row["etsy_listing_id"] is not None else None
    )

    last_failed = [
        e
        for e in read_all(conn, "listingdraft.push_failed")
        if e.user_id == user_id and e.payload.get("draft_id") == draft_id
    ]
    resume_stage = last_failed[-1].payload["stage"] if last_failed else "create"

    # Ranks already attached by a prior (partial) attempt -- _images_stage
    # emits one event per image, so this accumulates across however many
    # attempts happened, never just the last one.
    already_attached_ranks = {
        img["rank"]
        for e in read_all(conn, "listingdraft.images_attached")
        if e.user_id == user_id and e.payload.get("draft_id") == draft_id
        for img in e.payload.get("images", [])
    }

    stage = resume_stage
    try:
        if listing_id is None:
            stage = "create"
            listing_id = _create_stage(conn, user_id, adapter, cfg, draft_id, row, tags)
            resume_stage = "image"

        if resume_stage in ("create", "image"):
            stage = "image"
            _images_stage(
                conn,
                user_id,
                adapter,
                draft_id,
                listing_id,
                row["images_json"],
                skip_ranks=already_attached_ranks,
            )
        if resume_stage in ("create", "image", "file"):
            stage = "file"
            _file_stage(
                conn,
                user_id,
                adapter,
                draft_id,
                listing_id,
                landing_path,
                cfg.etsy.sellable_max_bytes,
            )

        stage = "update"
        _update_stage(adapter, listing_id, row, tags)
    except (EtsyWriteError, OSError) as exc:
        _emit_push_failed(conn, user_id, draft_id, listing_id, stage, exc)
        rebuild_listings(conn)
        return _card_for(conn, user_id, draft_id, cfg)

    # A retry resumed from "image"/"file"/"update" never re-emits
    # listingdraft.pushed_to_etsy (that would misrepresent history -- create
    # only ran once), so proj_listing_drafts.state would otherwise stay
    # stuck at "push_failed" forever after a successful resume.
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.push_resumed",
            payload={"draft_id": draft_id, "etsy_listing_id": listing_id},
        ),
    )
    rebuild_listings(conn)
    return _card_for(conn, user_id, draft_id, cfg)
