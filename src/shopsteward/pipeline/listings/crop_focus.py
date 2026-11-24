"""Operator crop-focus override (design 2026-11-21-multi-ratio-digital-files
§5): cropfocus.set / cropfocus.cleared events -> proj_crop_focus (folded in
projections.rebuild_listings, last write wins). Keyed by landing sha256."""

import sqlite3

from pydantic import BaseModel, Field

from shopsteward.core.events import Event, append


class CropFocusSet(BaseModel):
    landing_file_id: str
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


def set_crop_focus(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str, x: float, y: float
) -> None:
    """Raises pydantic.ValidationError (nothing appended) if x/y outside [0,1]."""
    payload = CropFocusSet(landing_file_id=landing_file_id, x=x, y=y).model_dump()
    append(conn, Event(user_id=user_id, type="cropfocus.set", payload=payload))


def clear_crop_focus(conn: sqlite3.Connection, user_id: int, landing_file_id: str) -> None:
    append(
        conn,
        Event(
            user_id=user_id, type="cropfocus.cleared", payload={"landing_file_id": landing_file_id}
        ),
    )


def get_crop_focus(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str
) -> tuple[float, float] | None:
    """Needs rebuild_listings() first."""
    row = conn.execute(
        "SELECT x, y FROM proj_crop_focus WHERE user_id=? AND landing_file_id=?",
        (user_id, landing_file_id),
    ).fetchone()
    return (row["x"], row["y"]) if row is not None else None
