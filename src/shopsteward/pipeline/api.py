"""APIRouter for /api/pipeline: scoring runs, Gate 1 decisions, landing scans.
Mounted by the top-level FastAPI app (shopsteward.api), mirroring editing/api.py."""

import os
import sqlite3

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.adapters.vision.fake import FixtureVisionAdapter
from shopsteward.adapters.vision.gemini import GeminiVisionAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.editing.projections import rebuild_editing
from shopsteward.pipeline import gate1, landing, tuning
from shopsteward.pipeline.config import COMMERCIAL_PROMPT_PATH, TUNING_PROFILE_PATH
from shopsteward.pipeline.live_gate import LIVE_VISION_ERROR, live_vision_open
from shopsteward.pipeline.models import Gate1Card, LandingReport, ScoringRunResult
from shopsteward.pipeline.projections import rebuild_pipeline
from shopsteward.pipeline.scoring import run_scoring
from shopsteward.settings import DEFAULT_USER_ID, bridge_dir, db_path

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


class ScoreRunRequest(BaseModel):
    limit: int | None = None
    live_vision: bool = False


class DecideRequest(BaseModel):
    photo_id: str
    decision: str


class UndoRequest(BaseModel):
    photo_id: str


def _connect() -> sqlite3.Connection:
    conn = connect(db_path())
    migrate(conn)
    return conn


@router.post("/score/run")
def score_run(request: ScoreRunRequest) -> ScoringRunResult:
    if request.live_vision and not live_vision_open():
        raise HTTPException(403, LIVE_VISION_ERROR)

    conn = _connect()
    try:
        rebuild_editing(conn)
        tuning.seed(conn, DEFAULT_USER_ID, TUNING_PROFILE_PATH)
        profile = tuning.get_profile(conn, DEFAULT_USER_ID)

        if request.live_vision:
            vision = GeminiVisionAdapter(
                api_key=os.environ["GEMINI_API_KEY"],
                prompt=COMMERCIAL_PROMPT_PATH.read_text(),
                pricing=profile.vision.est_cost_per_mtok,
            )
        else:
            vision = FixtureVisionAdapter()

        return run_scoring(
            conn, DEFAULT_USER_ID, vision, limit=request.limit, live=request.live_vision
        )
    finally:
        conn.close()


@router.get("/gate1/queue")
def gate1_queue(state: str = "pending") -> list[Gate1Card]:
    conn = _connect()
    try:
        rebuild_editing(conn)
        rebuild_pipeline(conn)
        return gate1.get_queue(conn, DEFAULT_USER_ID, state=state)
    finally:
        conn.close()


@router.get("/gate1/photo/{photo_id}/preview")
def gate1_preview(photo_id: str) -> FileResponse:
    conn = _connect()
    try:
        rebuild_editing(conn)  # ensure proj_photos exists even on a fresh DB
        row = conn.execute(
            "SELECT jpeg_path FROM proj_photos WHERE user_id = ? AND photo_id = ?",
            (DEFAULT_USER_ID, photo_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"unknown photo_id {photo_id!r}")
        return FileResponse(row["jpeg_path"])
    finally:
        conn.close()


@router.post("/gate1/decide")
def gate1_decide(request: DecideRequest) -> Gate1Card:
    conn = _connect()
    try:
        bridge = FolderBridge(bridge_dir())
        try:
            return gate1.decide(conn, DEFAULT_USER_ID, bridge, request.photo_id, request.decision)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@router.post("/gate1/undo")
def gate1_undo(request: UndoRequest) -> dict:
    conn = _connect()
    try:
        bridge = FolderBridge(bridge_dir())
        try:
            return gate1.undo(conn, DEFAULT_USER_ID, bridge, request.photo_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@router.post("/landing/scan")
def landing_scan() -> LandingReport:
    conn = _connect()
    try:
        tuning.seed(conn, DEFAULT_USER_ID, TUNING_PROFILE_PATH)
        return landing.scan_landing(conn, DEFAULT_USER_ID)
    finally:
        conn.close()
