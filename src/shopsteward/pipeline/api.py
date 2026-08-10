"""APIRouter for /api/pipeline: landing scans.
Mounted by the top-level FastAPI app (shopsteward.api), mirroring editing/api.py."""

import sqlite3

from fastapi import APIRouter

from shopsteward.core.db import connect, migrate
from shopsteward.pipeline import landing, tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.models import LandingReport
from shopsteward.settings import DEFAULT_USER_ID, db_path

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def _connect() -> sqlite3.Connection:
    conn = connect(db_path())
    migrate(conn)
    return conn


@router.post("/landing/scan")
def landing_scan() -> LandingReport:
    conn = _connect()
    try:
        tuning.seed(conn, DEFAULT_USER_ID, TUNING_PROFILE_PATH)
        return landing.scan_landing(conn, DEFAULT_USER_ID)
    finally:
        conn.close()
