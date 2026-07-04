"""API surface for the standalone editing module, mounted by the top-level
FastAPI app (shopsteward.api). Endpoints seed preset families and scan bridge
outcomes on demand, same as the CLI — no background daemon in M2."""

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.core.db import connect, migrate
from shopsteward.editing import presets
from shopsteward.editing.config import PRESET_FAMILIES_DIR, load_editing_defaults
from shopsteward.editing.dispatch import dispatch_edit_job
from shopsteward.editing.ingest import ingest_folder
from shopsteward.editing.models import IngestReport, PresetFamily
from shopsteward.editing.outcomes import scan_outcomes
from shopsteward.editing.projections import rebuild_editing
from shopsteward.settings import DEFAULT_USER_ID, bridge_dir, db_path

router = APIRouter(prefix="/api/editing", tags=["editing"])


class IngestRequest(BaseModel):
    path: str
    mode: str
    preset_family: str | None = None
    event: str | None = None
    output_folder: str | None = None


class IngestResponse(BaseModel):
    report: IngestReport
    edit_job_id: str | None = None


def _connect() -> sqlite3.Connection:
    conn = connect(db_path())
    migrate(conn)
    return conn


@router.post("/ingest")
def ingest(request: IngestRequest) -> IngestResponse:
    conn = _connect()
    try:
        presets.seed(conn, DEFAULT_USER_ID, PRESET_FAMILIES_DIR)

        # Validate before any side effects: mass mode needs a resolvable
        # preset family, so reject bad requests before ingesting anything.
        if request.mode != "hero":
            if not request.preset_family:
                raise HTTPException(400, "preset_family is required for mass mode")
            try:
                presets.get_family(conn, DEFAULT_USER_ID, request.preset_family)
            except KeyError as exc:
                raise HTTPException(400, str(exc.args[0])) from exc

        report = ingest_folder(
            conn,
            DEFAULT_USER_ID,
            Path(request.path),
            request.mode,
            preset_family=request.preset_family,
            event_name=request.event,
            output_folder=request.output_folder,
        )

        edit_job_id = None
        if request.mode != "hero" and report.photo_ids:
            editing_defaults = load_editing_defaults()
            output_folder = request.output_folder or str(
                Path(editing_defaults["event_output_root"]) / (request.event or "untitled")
            )
            bridge = FolderBridge(bridge_dir())
            job = dispatch_edit_job(
                conn,
                DEFAULT_USER_ID,
                bridge,
                photo_ids=report.photo_ids,
                preset_family=request.preset_family,
                mode=request.mode,
                event_name=request.event,
                output_folder=output_folder,
                editing_defaults=editing_defaults,
            )
            scan_outcomes(conn, DEFAULT_USER_ID, bridge)
            edit_job_id = job.job_id

        rebuild_editing(conn)
        return IngestResponse(report=report, edit_job_id=edit_job_id)
    finally:
        conn.close()


@router.get("/preset-families")
def preset_families() -> list[PresetFamily]:
    conn = _connect()
    try:
        presets.seed(conn, DEFAULT_USER_ID, PRESET_FAMILIES_DIR)
        return presets.list_families(conn, DEFAULT_USER_ID)
    finally:
        conn.close()


@router.get("/jobs")
def jobs() -> dict:
    conn = _connect()
    try:
        bridge = FolderBridge(bridge_dir())
        scan_outcomes(conn, DEFAULT_USER_ID, bridge)
        rebuild_editing(conn)

        ingest_jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM proj_ingest_jobs WHERE user_id=?", (DEFAULT_USER_ID,)
            ).fetchall()
        ]
        edit_jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM proj_edit_jobs WHERE user_id=?", (DEFAULT_USER_ID,)
            ).fetchall()
        ]
        photo_status_counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM proj_photos WHERE user_id=? GROUP BY status",
                (DEFAULT_USER_ID,),
            ).fetchall()
        }
        return {
            "ingest_jobs": ingest_jobs,
            "edit_jobs": edit_jobs,
            "photos": photo_status_counts,
        }
    finally:
        conn.close()
