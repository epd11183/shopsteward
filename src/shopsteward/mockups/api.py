"""APIRouter for /api/pipeline: mockup job runs, mockup listing/image serving,
staging template registry + annotate. Mounted by the top-level FastAPI app
(shopsteward.api), mirroring editing/api.py and pipeline/api.py."""

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from shopsteward.core.db import connect, migrate
from shopsteward.mockups.jobs import run_mockups
from shopsteward.mockups.models import (
    MockupJobResult,
    MockupRecord,
    StagingTemplate,
    TemplateReport,
)
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.mockups.templates import DEFAULT_TEMPLATES_DIR, scan_templates, write_sidecar
from shopsteward.settings import DEFAULT_USER_ID, db_path, mockups_dir, operator_templates_dir

router = APIRouter(prefix="/api/pipeline", tags=["mockups"])


class MockupRunRequest(BaseModel):
    photo_id: str | None = None
    force: bool = False


class TemplateAnnotateRequest(BaseModel):
    image_path: str
    sidecar: dict


def _connect() -> sqlite3.Connection:
    conn = connect(db_path())
    migrate(conn)
    return conn


def _under_any(path: Path, dirs: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(Path(d).resolve()) for d in dirs)


@router.post("/mockups/run")
def mockups_run(request: MockupRunRequest) -> MockupJobResult:
    conn = _connect()
    try:
        return run_mockups(conn, DEFAULT_USER_ID, photo_id=request.photo_id, force=request.force)
    finally:
        conn.close()


@router.get("/mockups")
def mockups_list(photo_id: str | None = None) -> list[MockupRecord]:
    conn = _connect()
    try:
        rebuild_mockups(conn)
        if photo_id is None:
            rows = conn.execute(
                "SELECT * FROM proj_mockups WHERE user_id=? ORDER BY path", (DEFAULT_USER_ID,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM proj_mockups WHERE user_id=? AND photo_id=? ORDER BY path",
                (DEFAULT_USER_ID, photo_id),
            ).fetchall()
        return [
            MockupRecord(
                path=row["path"],
                photo_id=row["photo_id"],
                landing_file_id=row["landing_file_id"],
                set_key=row["set_key"],
                intent=row["intent"],
                template_id=row["template_id"],
                params=json.loads(row["params_json"]) if row["params_json"] else {},
            )
            for row in rows
        ]
    finally:
        conn.close()


@router.get("/mockups/image")
def mockups_image(path: str) -> FileResponse:
    resolved = Path(path).resolve()
    if not _under_any(resolved, [mockups_dir()]):
        raise HTTPException(403, "path is outside the mockups directory")
    if not resolved.is_file():
        raise HTTPException(404, f"no such file: {path}")
    return FileResponse(resolved)


@router.get("/templates")
def templates_list() -> list[dict]:
    conn = _connect()
    try:
        rebuild_mockups(conn)
        rows = conn.execute(
            "SELECT * FROM proj_staging_templates WHERE user_id=? ORDER BY template_id",
            (DEFAULT_USER_ID,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@router.post("/templates/scan")
def templates_scan() -> TemplateReport:
    conn = _connect()
    try:
        return scan_templates(conn, DEFAULT_USER_ID)
    finally:
        conn.close()


@router.post("/templates/annotate")
def templates_annotate(request: TemplateAnnotateRequest) -> dict:
    conn = _connect()
    try:
        template = StagingTemplate.model_validate(request.sidecar)
        allowed_dirs = [DEFAULT_TEMPLATES_DIR, operator_templates_dir()]
        try:
            write_sidecar(Path(request.image_path), template, allowed_dirs=allowed_dirs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        report = scan_templates(conn, DEFAULT_USER_ID)

        row = conn.execute(
            "SELECT * FROM proj_staging_templates WHERE user_id=? AND template_id=?",
            (DEFAULT_USER_ID, template.template_id),
        ).fetchone()
        if row is None:
            return {"report": report.model_dump(), "template": None, "invalid_reason": None}
        if row["status"] == "invalid":
            return {
                "report": report.model_dump(),
                "template": None,
                "invalid_reason": row["reason"],
            }
        return {"report": report.model_dump(), "template": dict(row), "invalid_reason": None}
    finally:
        conn.close()


@router.get("/templates/image")
def templates_image(path: str) -> FileResponse:
    resolved = Path(path).resolve()
    if not _under_any(resolved, [DEFAULT_TEMPLATES_DIR, operator_templates_dir()]):
        raise HTTPException(403, "path is outside the template directories")
    if not resolved.is_file():
        raise HTTPException(404, f"no such file: {path}")
    return FileResponse(resolved)
