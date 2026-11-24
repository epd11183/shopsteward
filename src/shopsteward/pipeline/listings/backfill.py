"""Backfill multi-ratio digital files onto LIVE listings (design
2026-11-21-multi-ratio-digital-files section 8, slice 4: B0 dry-run, B1 live
upload, rollback).

NOTIFY-class (free, reversible, no search-ranking effect): CLI + events only,
no ops capability. The listing's existing rank-1 file is never touched.

Own event types (NOT files_planned/file_attached: the projection freezes plans
once files exist, so reuse would collide). No projection: state is read from
events and, for skip decisions, from Etsy's CURRENT file list.
  listingdraft.backfill_planned      {draft_id, etsy_listing_id, plan, digital_files_hash}
  listingdraft.file_backfilled       {draft_id, etsy_listing_id, etsy_file_id, key, rank,
                                      name, sizes, sha256}
  listingdraft.file_backfill_removed {draft_id, etsy_listing_id, etsy_file_id, key}

Failure policy: a failure on one listing is recorded in the report and the run
continues with the next listing (an upload failure stops THAT listing only).
"""

import json
import sqlite3
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.etsy.models import EtsyListing, ListingFile
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.listings.crop_focus import get_crop_focus
from shopsteward.pipeline.listings.digital_files import (
    DigitalFilesConfig,
    FileEntry,
    FilePlan,
    digital_files_hash,
    encode_file,
    photo_dims,
    plan_files,
)
from shopsteward.pipeline.listings.models import ListingConfig
from shopsteward.pipeline.listings.push import _file_name, _slug, panel_mismatch
from shopsteward.pipeline.listings.size_guard import undelivered_sizes
from shopsteward.pipeline.listings.source_assets import resolve_source

PLANNED = "listingdraft.backfill_planned"
BACKFILLED = "listingdraft.file_backfilled"
REMOVED = "listingdraft.file_backfill_removed"
_FULL = "full"


class BackfillRefused(RuntimeError):
    """A precondition failed before any encode, event or Etsy write."""


class ListingResult(BaseModel):
    listing_id: int
    # adopt_first | not_digital | planned_by_push | source_missing |
    # source_unreadable | etsy_error | no_files | refused | ok | uploaded |
    # failed | up_to_date | nothing_to_remove | removed
    status: str = ""
    detail: str = ""
    existing: list[str] = []
    to_upload: list[str] = []
    undelivered: list[str] = []
    panel: str | None = None
    copy_source: str = ""  # "synced" | "draft"
    changed: int = 0  # files uploaded / removed


class BackfillReport(BaseModel):
    results: list[ListingResult] = []

    @property
    def would_get(self) -> int:
        return sum(1 for r in self.results if r.status in ("ok", "uploaded") and r.to_upload)

    @property
    def loses_size(self) -> int:
        return sum(1 for r in self.results if r.undelivered)

    def skip_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            if r.status not in ("ok", "uploaded"):
                counts[r.status] = counts.get(r.status, 0) + 1
        return counts


def check_preconditions(cfg: ListingConfig, *, live: bool) -> DigitalFilesConfig:
    if cfg.digital_files is None:
        raise BackfillRefused(
            "digital_files config is null; nothing to plan (config sync-defaults)."
        )
    if live and not cfg.digital_files.copyright_holder:
        raise BackfillRefused("digital_files.copyright_holder is null; refusing a live backfill.")
    return cfg.digital_files


@dataclass
class _Ready:
    result: ListingResult
    draft_id: str = ""
    plan: FilePlan | None = None
    src: str = ""
    slug: str = ""
    files: list[ListingFile] = field(default_factory=list)
    todo: list[FileEntry] = field(default_factory=list)


def _snapshots(conn: sqlite3.Connection, user_id: int) -> dict[int, EtsyListing]:
    """Latest etsy.listing.observed per listing (seo_edit._latest_observed
    precedent) -- the only stored source of live tags/description."""
    out: dict[int, EtsyListing] = {}
    for e in read_live_observed(conn, "etsy.listing.observed"):
        if e.user_id != user_id:
            continue
        try:
            out[e.payload["listing_id"]] = EtsyListing.model_validate(e.payload)
        except (ValidationError, KeyError):
            continue
    return out


def _inspect(
    conn: sqlite3.Connection,
    user_id: int,
    dcfg: DigitalFilesConfig,
    adapter: EtsyWriteAdapter,
    lid: int,
    proj_title: str | None,
    snap: EtsyListing | None,
) -> _Ready:
    res = ListingResult(listing_id=lid)

    def stop(status: str, detail: str = "") -> _Ready:
        res.status, res.detail = status, detail
        return _Ready(res)

    rows = conn.execute(
        "SELECT draft_id, provider, listing_type, files_plan_json, set_key, title, "
        "description, tags_json FROM proj_listing_drafts WHERE user_id=? "
        "AND etsy_listing_id IS NOT NULL AND CAST(etsy_listing_id AS INTEGER)=? "
        "ORDER BY (landing_file_id IS NULL), draft_id",
        (user_id, lid),
    ).fetchall()
    if not rows:
        return stop("adopt_first", "no draft row; run `archive adopt-local` first")
    # NOTE: this provider/type check does nothing for ADOPTED rows (provider is
    # always etsy_digital, listing_type NULL). The real guards against touching a
    # physical or unexpected listing are the `no_files` rule and the
    # exactly-one-unknown-file rule below. It only filters pipeline-built POD drafts.
    if any(
        r["provider"] != "etsy_digital" or r["listing_type"] not in (None, "download") for r in rows
    ):
        return stop("not_digital")
    if any(r["files_plan_json"] for r in rows):
        return stop("planned_by_push", "belongs to the push/gate3 retry path")
    row = rows[0]

    ref = resolve_source(conn, user_id, lid)
    if ref is None or not ref.on_disk_present or ref.on_disk_path is None:
        archived = ref is not None and ref.archived
        return stop("source_missing", f"archived: {'yes' if archived else 'no'}")
    try:
        files = adapter.list_listing_files(lid)
    except EtsyWriteError as exc:
        return stop("etsy_error", str(exc))
    res.existing = [f"{f.rank}:{f.name}" for f in sorted(files, key=lambda f: f.rank or 0)]
    if not files:
        return stop("no_files", "no files (physical?); never uploaded")
    try:
        dims = photo_dims(ref.on_disk_path)
    except OSError as exc:
        return stop("source_unreadable", str(exc))

    focus = (
        get_crop_focus(conn, user_id, ref.landing_file_id) if ref.landing_file_id else None
    ) or (0.5, 0.5)
    plan = plan_files(dims, focus, dcfg)
    slug = _slug(ref.on_disk_path)

    synced = snap is not None and snap.description is not None
    description = snap.description if synced and snap else (row["description"] or "")
    tags = snap.tags if snap else json.loads(row["tags_json"] or "[]")
    res.copy_source = "synced" if synced else "draft"
    title = proj_title or row["title"] or ""
    res.undelivered = undelivered_sizes([title, description or "", *tags], plan, dcfg)
    res.panel = panel_mismatch(conn, user_id, row["set_key"], plan)

    planned = {_file_name(slug, e).lower() for e in plan.files}
    on_etsy = {(f.name or "").lower() for f in files}
    todo = [
        e
        for e in sorted(plan.files, key=lambda e: e.rank)
        if e.key != _FULL and _file_name(slug, e).lower() not in on_etsy
    ]
    res.to_upload = [_file_name(slug, e) for e in todo]
    unknown = [f for f in files if (f.name or "").lower() not in planned]
    if len(unknown) != 1:
        res.status = "refused"
        res.detail = f"expected exactly 1 legacy file, found {len(unknown)} unrecognised"
    elif len(files) + len(todo) > dcfg.max_files:
        res.status = "refused"
        res.detail = f"{len(files)} existing + {len(todo)} new exceeds max_files={dcfg.max_files}"
    else:
        res.status = "ok" if todo else "up_to_date"
        outstanding, matches = _drift_state(_events(conn, user_id, lid), files, plan, dcfg)
        if todo and outstanding and not matches:
            res.status, res.detail = "refused", "file plan drift since the partial backfill"
    return _Ready(res, row["draft_id"], plan, ref.on_disk_path, slug, files, todo)


def _events(conn: sqlite3.Connection, user_id: int, lid: int) -> list[Event]:
    evs = read_all(conn, PLANNED) + read_all(conn, "listingdraft.file_backfill")
    return sorted(
        (
            e
            for e in evs
            if e.user_id == user_id
            and e.type in (PLANNED, BACKFILLED, REMOVED)
            and int(e.payload["etsy_listing_id"]) == lid
        ),
        key=lambda e: e.id or 0,
    )


def _emit(conn: sqlite3.Connection, user_id: int, type_: str, payload: dict) -> None:
    append(conn, Event(user_id=user_id, type=type_, payload=payload))


def _recorded_plan(last: dict | None) -> FilePlan | None:
    try:
        return FilePlan.model_validate(last["plan"]) if last is not None else None
    except (ValidationError, KeyError, TypeError):
        return None


def _drift_state(
    events: list[Event], files: list[ListingFile], plan: FilePlan, dcfg: DigitalFilesConfig
) -> tuple[set[int], bool]:
    """(backfilled files still on Etsy, does the latest recorded plan equal this one).
    Outstanding files bind us to the recorded plan: a config or focus change must
    not mix crop sets. With none outstanding, re-planning is free."""
    removed = {e.payload["etsy_file_id"] for e in events if e.type == REMOVED}
    on_etsy = {f.listing_file_id for f in files}
    outstanding = (
        {e.payload["etsy_file_id"] for e in events if e.type == BACKFILLED} - removed
    ) & on_etsy
    last = next((e.payload for e in reversed(events) if e.type == PLANNED), None)
    matches = (
        last is not None
        and last.get("digital_files_hash") == digital_files_hash(dcfg)
        and _recorded_plan(last) == plan
    )
    return outstanding, matches


def _reconcile(conn: sqlite3.Connection, user_id: int, rd: _Ready) -> None:
    """A timed-out/5xx upload may have been kept by Etsy with no event. With a
    recorded plan, record (reconciled) every file on Etsy that carries a planned
    non-full name but has no file_backfilled event, so rollback can find it."""
    lid = rd.result.listing_id
    events = _events(conn, user_id, lid)
    last = next((e.payload for e in reversed(events) if e.type == PLANNED), None)
    recorded = _recorded_plan(last)
    if recorded is None:
        return
    known = {e.payload["etsy_file_id"] for e in events if e.type == BACKFILLED}
    by_name = {_file_name(rd.slug, e).lower(): e for e in recorded.files if e.key != _FULL}
    for f in rd.files:
        entry = by_name.get((f.name or "").lower())
        if entry is not None and f.listing_file_id not in known:
            _emit(
                conn,
                user_id,
                BACKFILLED,
                {
                    "draft_id": rd.draft_id,
                    "etsy_listing_id": lid,
                    "etsy_file_id": f.listing_file_id,
                    "key": entry.key,
                    "rank": entry.rank,
                    "name": f.name,
                    "sizes": entry.sizes,
                    "sha256": None,
                    "reconciled": True,
                },
            )


def _apply(
    conn: sqlite3.Connection,
    user_id: int,
    dcfg: DigitalFilesConfig,
    adapter: EtsyWriteAdapter,
    rd: _Ready,
) -> None:
    res, plan, lid = rd.result, rd.plan, rd.result.listing_id
    assert plan is not None
    plan_hash = digital_files_hash(dcfg)
    events = _events(conn, user_id, lid)
    outstanding, matches = _drift_state(events, rd.files, plan, dcfg)
    if outstanding and not matches:
        res.status, res.detail = "refused", "file plan drift since the partial backfill"
        return

    # Encode EVERYTHING before the first upload.
    prepared: list[tuple[FileEntry, str, bytes, str]] = []
    try:
        for e in rd.todo:
            data, sha = encode_file(rd.src, e, dcfg)
            prepared.append((e, _file_name(rd.slug, e), data, sha))
    except (OSError, ValueError) as exc:
        res.status, res.detail = "failed", f"encode failed: {exc}"
        return

    if not matches:
        _emit(
            conn,
            user_id,
            PLANNED,
            {
                "draft_id": rd.draft_id,
                "etsy_listing_id": lid,
                "plan": plan.model_dump(mode="json"),
                "digital_files_hash": plan_hash,
            },
        )
    before = {f.listing_file_id for f in rd.files}
    for entry, name, data, sha in prepared:
        payload = {
            "draft_id": rd.draft_id,
            "etsy_listing_id": lid,
            "key": entry.key,
            "rank": entry.rank,
            "name": name,
            "sizes": entry.sizes,
            "sha256": sha,
        }
        try:
            ref = adapter.upload_listing_file(lid, data, name=name, rank=entry.rank)
        except EtsyWriteError as exc:
            res.status, res.detail = "failed", f"upload of {name} failed: {exc}"
            if exc.status_code == 0 or exc.status_code >= 500:
                # Etsy may have kept it -- record it for rollback (a failed re-list
                # is healed by _reconcile on the next run).
                try:
                    kept = next(
                        (
                            f
                            for f in adapter.list_listing_files(lid)
                            if (f.name or "").lower() == name.lower()
                            and f.listing_file_id not in before
                        ),
                        None,
                    )
                except EtsyWriteError:
                    kept = None
                if kept is not None:
                    _emit(
                        conn,
                        user_id,
                        BACKFILLED,
                        {**payload, "etsy_file_id": kept.listing_file_id, "reconciled": True},
                    )
            return
        _emit(conn, user_id, BACKFILLED, {**payload, "etsy_file_id": ref.listing_file_id})
        before.add(ref.listing_file_id)
        res.changed += 1
    res.status = "uploaded"


def backfill(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: ListingConfig,
    adapter: EtsyWriteAdapter,
    *,
    listing_id: int | None = None,
    live: bool = False,
) -> BackfillReport:
    """B0 (live=False): read-only report, no uploads/deletes/events. B1
    (live=True): upload ranks 2..N. Needs fresh projections (listings,
    pipeline, mockups, core)."""
    dcfg = check_preconditions(cfg, live=live)
    titles = {
        r["listing_id"]: (r["title"], r["state"])
        for r in conn.execute(
            "SELECT listing_id, title, state FROM proj_listings WHERE user_id=?", (user_id,)
        )
    }
    ids = (
        [listing_id]
        if listing_id is not None
        else sorted(i for i, (_, state) in titles.items() if state == "active")
    )
    snaps = _snapshots(conn, user_id)
    report = BackfillReport()
    for lid in ids:
        title = titles.get(lid, (None, ""))[0]
        rd = _inspect(conn, user_id, dcfg, adapter, lid, title, snaps.get(lid))
        # Only on listings that passed every fail-closed check: a refused listing
        # may carry a hand-uploaded file that merely shares one of our names.
        if live and rd.plan is not None and rd.result.status in ("ok", "up_to_date"):
            _reconcile(conn, user_id, rd)
        if live and rd.result.status == "ok" and rd.todo:
            _apply(conn, user_id, dcfg, adapter, rd)
        report.results.append(rd.result)
    return report


def rollback(
    conn: sqlite3.Connection, user_id: int, adapter: EtsyWriteAdapter, listing_id: int
) -> ListingResult:
    """Delete only files THIS backfill uploaded (file_backfilled events) that are
    still on Etsy. Refuses if no other file would remain: Etsy converts a digital
    listing that loses its last file into a physical one."""
    res = ListingResult(listing_id=listing_id)
    events = _events(conn, user_id, listing_id)
    gone = {e.payload["etsy_file_id"] for e in events if e.type == REMOVED}
    mine = {e.payload["etsy_file_id"]: e.payload for e in events if e.type == BACKFILLED}
    try:
        files = adapter.list_listing_files(listing_id)
    except EtsyWriteError as exc:
        res.status, res.detail = "etsy_error", str(exc)
        return res
    present = [f for f in files if f.listing_file_id in mine and f.listing_file_id not in gone]
    if not present:
        res.status = "nothing_to_remove"
    elif len(files) - len(present) < 1:
        res.status, res.detail = (
            "refused",
            "no other file would remain (Etsy would convert to physical)",
        )
    else:
        for f in present:
            try:
                adapter.delete_listing_file(listing_id, f.listing_file_id)
            except EtsyWriteError as exc:
                res.status, res.detail = (
                    "failed",
                    f"delete of file {f.listing_file_id} failed: {exc}",
                )
                return res
            p = mine[f.listing_file_id]
            _emit(
                conn,
                user_id,
                REMOVED,
                {
                    "draft_id": p["draft_id"],
                    "etsy_listing_id": listing_id,
                    "etsy_file_id": f.listing_file_id,
                    "key": p["key"],
                },
            )
            res.changed += 1
        res.status = "removed"
    return res
