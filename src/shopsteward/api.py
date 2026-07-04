"""FastAPI backend serving the local dashboard and analytics API."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import Summary, analytics_summary
from shopsteward.settings import db_path

DEFAULT_USER_ID = 1  # single-operator v1; schema stays multi-tenant-ready


def create_app() -> FastAPI:
    app = FastAPI(title="ShopSteward")

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/analytics/summary")
    def summary() -> Summary:
        conn = connect(db_path())
        try:
            migrate(conn)
            return analytics_summary(conn, user_id=DEFAULT_USER_ID)
        finally:
            conn.close()

    dist = Path("frontend/dist")
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app
