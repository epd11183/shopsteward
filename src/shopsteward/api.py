"""FastAPI backend serving the local dashboard and analytics API."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import Summary, analytics_summary, rebuild
from shopsteward.editing.api import router as editing_router
from shopsteward.mockups.api import router as mockups_router
from shopsteward.pipeline.api import router as pipeline_router
from shopsteward.settings import DEFAULT_USER_ID, db_path


def create_app() -> FastAPI:
    app = FastAPI(title="ShopSteward")
    app.include_router(editing_router)
    app.include_router(pipeline_router)
    app.include_router(mockups_router)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/analytics/summary")
    def summary() -> Summary:
        conn = connect(db_path())
        try:
            migrate(conn)
            # Projections live in derived tables; rebuilding here is cheap at
            # this scale and guarantees the endpoint works on a fresh DB.
            rebuild(conn)
            return analytics_summary(conn, user_id=DEFAULT_USER_ID)
        finally:
            conn.close()

    dist = Path("frontend/dist")
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app
