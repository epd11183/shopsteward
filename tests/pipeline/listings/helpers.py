"""Shared fixture builders for listings tests -- plain importable helpers,
not a conftest.py fixture module (mockups/helpers.py precedent)."""

import sqlite3

from shopsteward.core.events import Event, append
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1


def seed_landing_file_with_mockup_set(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    photo_id: str,
    path: str,
    set_key: str,
    intents: list[str],
    user_id: int = USER_ID,
) -> None:
    """Appends a valid landing.file_observed + a completed mockup set (one
    mockup.generated per intent, in the given order, then
    mockupset.completed) and rebuilds the pipeline/mockups projections the
    listings build stage reads via raw SQL."""
    append(
        conn,
        Event(
            user_id=user_id,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": path,
                "base_name": None,
                "format": "JPEG",
                "width": 4000,
                "height": 3000,
                "color_space": "sRGB",
                "photo_id": photo_id,
            },
        ),
    )
    for i, intent in enumerate(intents):
        append(
            conn,
            Event(
                user_id=user_id,
                type="mockup.generated",
                payload={
                    "photo_id": photo_id,
                    "landing_file_id": file_id,
                    "set_key": set_key,
                    "intent": intent,
                    "template_id": None,
                    "path": f"/mockups/{photo_id}/{intent}_{i}.jpg",
                    "params": {},
                },
            ),
        )
    append(
        conn,
        Event(
            user_id=user_id,
            type="mockupset.completed",
            payload={
                "photo_id": photo_id,
                "landing_file_id": file_id,
                "set_key": set_key,
                "count": len(intents),
                "config_hash": "mockup-cfg-hash",
                "template_library_hash": "template-lib-hash",
            },
        ),
    )
    rebuild_pipeline(conn)
    rebuild_mockups(conn)
