"""APIRouter for /api/pipeline: listings build + Gate 3 (design §8). Mounted
by the top-level FastAPI app (shopsteward.api), mirroring pipeline/api.py and
mockups/api.py.

Live flags on /listings/build are still checked here (403 unless the gate is
open) so a live run can be triggered offline in tests without an env var --
Gate 3's edit/publish/retry endpoints, by contrast, take no live flag from the
frontend at all: the write adapter's liveness is decided solely by
live_etsy_write_open() (PRD §13 decision 41), never by client input."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.pipeline.listings import gate3
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.models import BuildReport, Gate3Card
from shopsteward.pipeline.listings.pricing import BelowFloor
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.push import build_etsy_write_adapter
from shopsteward.pipeline.live_gate import (
    live_copy_error,
    live_copy_open,
    live_etsy_write_error,
    live_etsy_write_open,
)
from shopsteward.settings import DEFAULT_USER_ID, db_path, mockups_dir

router = APIRouter(prefix="/api/pipeline", tags=["listings"])

# ponytail: FakeEtsyWriteAdapter is an in-memory dict, so a fresh instance
# per request would make a draft pushed by /listings/build invisible to a
# later /gate3/edit|publish|retry call (separate dict, "unknown listing_id").
# One instance per db_path, alive for this server process, keeps Fake-mode
# offline dev-preview coherent end to end; it resets on process restart --
# fine, since Fake mode was never meant to be durable. Live mode is
# unaffected (LiveEtsyWriteAdapter talks to the real, persistent Etsy API,
# so a fresh instance per request is already correct there).
_fake_adapters: dict[str, FakeEtsyWriteAdapter] = {}


class ListingsBuildRequest(BaseModel):
    photo_id: str | None = None
    force: bool = False
    live_copy: bool = False
    live_etsy_write: bool = False


class Gate3EditRequest(BaseModel):
    draft_id: str
    title: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    price: float | None = None


class Gate3DraftRequest(BaseModel):
    draft_id: str


def _connect() -> sqlite3.Connection:
    conn = connect(db_path())
    migrate(conn)
    return conn


def _fake_adapter() -> FakeEtsyWriteAdapter:
    key = str(db_path())
    if key not in _fake_adapters:
        _fake_adapters[key] = FakeEtsyWriteAdapter()
    return _fake_adapters[key]


def _write_adapter_for_build(live: bool) -> EtsyWriteAdapter:
    # live here is the caller-supplied (already-gate-checked) request flag,
    # not live_etsy_write_open() directly -- an operator with the env gate
    # open but no --live-etsy-write/live_etsy_write:true on this particular
    # request must still get the offline Fake adapter.
    return build_etsy_write_adapter(live=live) if live else _fake_adapter()


def _write_adapter() -> EtsyWriteAdapter:
    # Gate 3 endpoints: no flag from the frontend can force live -- the env
    # gate alone decides (PRD §13 decision 41).
    return build_etsy_write_adapter(live=True) if live_etsy_write_open() else _fake_adapter()


@router.post("/listings/build")
def listings_build(request: ListingsBuildRequest) -> BuildReport:
    if request.live_copy and not live_copy_open():
        raise HTTPException(403, live_copy_error())
    if request.live_etsy_write and not live_etsy_write_open():
        raise HTTPException(403, live_etsy_write_error())

    conn = _connect()
    try:
        return build_drafts(
            conn,
            DEFAULT_USER_ID,
            photo_id=request.photo_id,
            force=request.force,
            live_copy=request.live_copy,
            live_etsy_write=request.live_etsy_write,
            etsy_adapter=_write_adapter_for_build(request.live_etsy_write),
        )
    finally:
        conn.close()


@router.get("/gate3/queue")
def gate3_queue() -> list[Gate3Card]:
    conn = _connect()
    try:
        return gate3.queue(conn, DEFAULT_USER_ID)
    finally:
        conn.close()


@router.get("/gate3/draft/{draft_id}/image")
def gate3_draft_image(draft_id: str, path: str) -> FileResponse:
    # draft_id is unused beyond scoping the URL to a draft (the M4 precedent
    # route mockups/api.py's /mockups/image is keyed the same way -- path is
    # what's actually resolved/validated); kept in the path for symmetry with
    # the other /gate3/draft/{draft_id}/... routes and so a leaked/typo'd
    # path can't be requested without a draft context.
    del draft_id
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(mockups_dir().resolve()):
        raise HTTPException(403, "path is outside the mockups directory")
    if not resolved.is_file():
        raise HTTPException(404, f"no such file: {path}")
    return FileResponse(resolved)


@router.post("/gate3/edit")
def gate3_edit(request: Gate3EditRequest) -> Gate3Card:
    conn = _connect()
    try:
        fields = request.model_dump(exclude={"draft_id"}, exclude_none=True)
        try:
            return gate3.edit(conn, DEFAULT_USER_ID, request.draft_id, fields, _write_adapter())
        except BelowFloor as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@router.post("/gate3/publish")
def gate3_publish(request: Gate3DraftRequest) -> Gate3Card:
    conn = _connect()
    try:
        try:
            return gate3.publish(conn, DEFAULT_USER_ID, request.draft_id, _write_adapter())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@router.post("/gate3/retry")
def gate3_retry(request: Gate3DraftRequest) -> Gate3Card:
    conn = _connect()
    try:
        rebuild_listings(conn)
        row = conn.execute(
            "SELECT state FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
            (DEFAULT_USER_ID, request.draft_id),
        ).fetchone()
        state = row["state"] if row is not None else None
        adapter = _write_adapter()
        try:
            if state == "push_failed":
                return gate3.retry_push(conn, DEFAULT_USER_ID, request.draft_id, adapter)
            if state == "publish_failed":
                return gate3.publish(conn, DEFAULT_USER_ID, request.draft_id, adapter)
            raise HTTPException(400, f"draft {request.draft_id!r} has nothing to retry")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()
