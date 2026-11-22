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
import re
import sqlite3
from pathlib import Path

from shopsteward.adapters.etsy.auth import EtsyTokenStore
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.etsy.live import LiveEtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyDraftSpec, EtsyListingUpdate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.copy import copy_sizes_problem
from shopsteward.pipeline.listings.digital_files import (
    FileEntry,
    FilePlan,
    digital_files_hash,
    encode_file,
    photo_dims,
    plan_files,
    plan_sizes,
)
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


def photo_has_digital_listing(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str | None
) -> str | None:
    """draft_id of a DIGITAL draft (incl. adopted live listings) for this
    photo that already has an Etsy listing, else None. POD rows share
    landing_file_id but are a different product, so they never block. Needs
    a fresh rebuild_listings()."""
    if landing_file_id is None:
        return None
    row = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND landing_file_id=? "
        "AND provider='etsy_digital' AND etsy_listing_id IS NOT NULL LIMIT 1",
        (user_id, landing_file_id),
    ).fetchone()
    return row["draft_id"] if row is not None else None


def _eligible_drafts(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT draft_id, landing_file_id, title, tags_json, description, price, currency, "
        "images_json, files_plan_json, file_source, set_key "
        "FROM proj_listing_drafts WHERE user_id=? "
        "AND title IS NOT NULL "
        "AND price IS NOT NULL AND etsy_listing_id IS NULL AND state != 'published' "
        # Newest first per photo so the current-config draft is the one that
        # pushes (rowid breaks created_at ties: the projection inserts in
        # event order and ON CONFLICT updates keep the rowid).
        "ORDER BY created_at DESC, rowid DESC, draft_id",
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


class FilePlanDriftError(RuntimeError):
    """The re-planned files differ from the plan recorded at build time (or the
    plan can't be honoured safely)."""


def panel_mismatch(
    conn: sqlite3.Connection, user_id: int, set_key: str | None, plan: FilePlan
) -> str | None:
    """Design §4/§7: the "what you get" panel must show exactly the sizes the
    file plan delivers. "legacy" = the panel never recorded its sizes,
    "mismatch" = it recorded different ones, None = fine (or no panel)."""
    for row in conn.execute(
        "SELECT params_json FROM proj_mockups WHERE user_id=? AND set_key=? "
        "AND intent='digital_whatyougot' ORDER BY path",
        (user_id, set_key),
    ):
        recorded = json.loads(row["params_json"] or "{}").get("sizes")
        if recorded is None:
            return "legacy"
        if recorded != plan_sizes(plan):
            return "mismatch"
    return None


def check_sizes_fresh(
    conn: sqlite3.Connection, user_id: int, cfg: ListingConfig, row: sqlite3.Row
) -> None:
    """Before anything is created on Etsy: stored copy must not name a size the
    recorded plan does not deliver (the appended Files-included block passes by
    construction) and the panel must match the plan. Drafts built before slice
    3b can fail this; the fix is `--force` + re-rendered mockups. No-op without
    digital_files or a recorded plan; a malformed plan is left to
    _checked_plan's own drift error."""
    if cfg.digital_files is None or row["files_plan_json"] is None:
        return
    try:
        plan = FilePlan.model_validate(
            {k: json.loads(row["files_plan_json"])[k] for k in ("source_dims", "focus", "files")}
        )
    except (ValueError, KeyError, TypeError):
        return
    bad = copy_sizes_problem(
        cfg, plan, row["title"], row["description"] or "", json.loads(row["tags_json"] or "[]")
    )
    if bad or panel_mismatch(conn, user_id, row["set_key"], plan) is not None:
        raise FilePlanDriftError("copy/panel sizes stale: rebuild with --force")


# (entry, upload name, bytes, sha256). ponytail: all entries are held in memory
# (<= 5 x 20 MB); stream from disk if that ever matters.
PreparedFile = tuple[FileEntry, str, bytes, str]


def _slug(landing_path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", Path(landing_path).stem.lower()).strip("-")[:40] or "photo"


def _checked_plan(
    cfg: ListingConfig, files_plan_json: str | None, landing_path: str
) -> FilePlan | None:
    """The recorded plan if a re-plan still yields it; None when the draft has
    no plan or digital_files is off (legacy single-file path)."""
    if files_plan_json is None or cfg.digital_files is None:
        return None
    try:
        recorded = json.loads(files_plan_json)
        expected = FilePlan.model_validate(
            {k: recorded[k] for k in ("source_dims", "focus", "files")}
        )
        recorded_hash = recorded["digital_files_hash"]
    except (ValueError, KeyError, TypeError) as exc:  # JSONDecodeError/ValidationError
        raise FilePlanDriftError("file plan drift: malformed recorded plan") from exc
    replanned = plan_files(photo_dims(landing_path), expected.focus, cfg.digital_files)
    if replanned.files != expected.files or recorded_hash != digital_files_hash(cfg.digital_files):
        raise FilePlanDriftError("file plan drift")
    return replanned


def _prepare_files(
    cfg: ListingConfig,
    files_plan_json: str | None,
    landing_path: str,
    skip_keys: set[str] | frozenset[str] = frozenset(),
) -> list[PreparedFile] | None:
    """Everything that can fail locally, BEFORE anything is created/uploaded:
    drift check + encode of every entry not already attached. None = legacy
    single-file path. Rolling digital_files back to None after files were
    already attached is drift (the legacy path would duplicate `full`)."""
    if files_plan_json is not None and cfg.digital_files is None and skip_keys:
        raise FilePlanDriftError("file plan drift: digital_files disabled after partial push")
    plan = _checked_plan(cfg, files_plan_json, landing_path)
    if plan is None:
        return None
    assert cfg.digital_files is not None
    slug = _slug(landing_path)
    out: list[PreparedFile] = []
    for entry in sorted(plan.files, key=lambda f: f.rank):
        if entry.key in skip_keys:
            continue
        data, sha = encode_file(landing_path, entry, cfg.digital_files)
        out.append((entry, _file_name(slug, entry), data, sha))
    return out


def _reconcile_files(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyWriteAdapter,
    draft_id: str,
    listing_id: int,
    cfg: ListingConfig,
    files_plan_json: str | None,
    landing_path: str,
    skip_keys: set[str],
) -> None:
    """A timed-out upload may have been accepted by Etsy. Before a retry
    uploads anything, ask Etsy which planned filenames already exist and record
    a reconciled file_attached for each (adding its key to skip_keys, in place).
    list_listing_files failing propagates: better a failed retry than duplicates."""
    plan = _checked_plan(cfg, files_plan_json, landing_path)
    if plan is None:
        return
    slug = _slug(landing_path)
    etsy_files = adapter.list_listing_files(listing_id)
    on_etsy = {(f.name or "").lower(): f for f in etsy_files}
    for entry in sorted(plan.files, key=lambda f: f.rank):
        name = _file_name(slug, entry)
        found = on_etsy.get(name.lower())
        if entry.key in skip_keys or found is None:
            continue
        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.file_attached",
                payload={
                    "draft_id": draft_id,
                    "etsy_listing_id": listing_id,
                    "etsy_file_id": found.listing_file_id,
                    "source": "digital_files",
                    "sha256": None,
                    "key": entry.key,
                    "rank": entry.rank,
                    "name": name,
                    "sizes": entry.sizes,
                    "reconciled": True,
                },
            ),
        )
        skip_keys.add(entry.key)
    # Fail closed: a file on the listing we can't account for (renamed after a
    # lost response?) would be duplicated by the retry.
    if len(etsy_files) > len(skip_keys):
        raise FilePlanDriftError("unreconciled files on listing")


def _file_name(slug: str, entry: FileEntry) -> str:
    return "_".join([slug, entry.key, *(["-".join(entry.sizes)] if entry.sizes else [])]) + ".jpg"


def _file_stage(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyWriteAdapter,
    draft_id: str,
    listing_id: int,
    landing_path: str,
    sellable_max_bytes: int,
    *,
    prepared: list[PreparedFile] | None = None,
    file_source: str | None = None,
    skip_keys: set[str] | frozenset[str] = frozenset(),
) -> None:
    """prepared (from _prepare_files): upload each entry in rank order, one
    file_attached event per upload (already-attached keys were excluded at
    prepare time). None: the legacy single file, skipped if already attached."""
    if prepared is None:
        if "full" in skip_keys:
            return
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
        return

    for entry, name, data, sha in prepared:
        file_ref = adapter.upload_listing_file(listing_id, data, name=name, rank=entry.rank)
        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.file_attached",
                payload={
                    "draft_id": draft_id,
                    "etsy_listing_id": listing_id,
                    "etsy_file_id": file_ref.listing_file_id,
                    "source": file_source or "digital_files",
                    "sha256": sha,
                    "key": entry.key,
                    "rank": entry.rank,
                    "name": name,
                    "sizes": entry.sizes,
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
        # Drift check + encode of every file BEFORE creating anything on Etsy,
        # so a local failure never strands a half-built listing.
        check_sizes_fresh(conn, user_id, cfg, row)
        stage = "file"
        prepared = _prepare_files(cfg, row["files_plan_json"], landing_path)
        stage = "create"
        listing_id = _create_stage(conn, user_id, adapter, cfg, draft_id, row, tags)

        stage = "image"
        _images_stage(conn, user_id, adapter, draft_id, listing_id, row["images_json"])

        stage = "file"
        _file_stage(
            conn,
            user_id,
            adapter,
            draft_id,
            listing_id,
            landing_path,
            cfg.etsy.sellable_max_bytes,
            prepared=prepared,
            file_source=row["file_source"],
        )

        stage = "update"
        _update_stage(adapter, listing_id, row, tags)
        return True
    except (EtsyWriteError, OSError, FilePlanDriftError) as exc:
        _emit_push_failed(conn, user_id, draft_id, listing_id, stage, exc)
        return False


def push_drafts(
    conn: sqlite3.Connection, user_id: int, cfg: ListingConfig, adapter: EtsyWriteAdapter
) -> BuildReport:
    """Push every fully-built, not-yet-pushed draft. A failure on one draft
    is isolated -- it never aborts the batch."""
    rebuild_listings(conn)
    report = BuildReport()
    # One listing per photo: never push a second draft for a landing file that
    # already has a listing (from before this run, or earlier in it). The
    # projection is fresh from the rebuild above; `attempted` covers this run.
    attempted: set[str] = set()
    for row in _eligible_drafts(conn, user_id):
        landing_id = row["landing_file_id"]
        if landing_id is not None:
            if photo_has_digital_listing(conn, user_id, landing_id) or landing_id in attempted:
                report.skipped_already_pushed += 1
                continue
            attempted.add(landing_id)
        if _push_one(conn, user_id, adapter, cfg, row):
            report.pushed += 1
        else:
            report.push_failed += 1
    rebuild_listings(conn)
    return report
