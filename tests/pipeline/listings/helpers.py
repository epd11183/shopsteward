"""Shared fixture builders for listings tests -- plain importable helpers,
not a conftest.py fixture module (mockups/helpers.py precedent)."""

import sqlite3
from pathlib import Path

from PIL import Image

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
    mockups_dir: Path,
    user_id: int = USER_ID,
) -> None:
    """Appends a valid landing.file_observed + a completed mockup set (one
    mockup.generated per intent, in the given order, then
    mockupset.completed) and rebuilds the pipeline/mockups projections the
    listings build stage reads via raw SQL. Writes a real tiny JPEG at each
    mockup path (under mockups_dir) -- the push stage (M5a slice 3) reads
    these bytes off disk, so a placeholder path is no longer enough."""
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
        mockup_path = Path(mockups_dir) / photo_id / f"{intent}_{i}.jpg"
        mockup_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (50, 50), (i, i, i)).save(mockup_path, "JPEG")
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
                    "path": str(mockup_path),
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
