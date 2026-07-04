"""Tuning-profile store: seed config/defaults/tuning_profile.json into the
event log, then read back last-write-wins by name. Mirrors editing/presets.py."""

import json
import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.models import TuningProfile

TUNING_EVENT_TYPES = ("tuningprofile.seeded", "tuningprofile.updated")


def _latest_by_name(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for e in read_all(conn, "tuningprofile."):
        if e.user_id != user_id or e.type not in TUNING_EVENT_TYPES:
            continue
        latest[e.payload["name"]] = e.payload["profile"]
    return latest


def seed(conn: sqlite3.Connection, user_id: int, path: Path) -> bool:
    profile = TuningProfile.model_validate(json.loads(Path(path).read_text()))
    profile_dump = profile.model_dump(by_alias=True)

    # Seed only when this profile name has never been written for the user.
    # Comparing against the latest payload would silently re-seed defaults
    # over a future operator `tuningprofile.updated` (last-write-wins).
    if profile.name in _latest_by_name(conn, user_id):
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="tuningprofile.seeded",
            payload={"name": profile.name, "profile": profile_dump, "source": "defaults"},
        ),
    )
    return True


def get_profile(conn: sqlite3.Connection, user_id: int, name: str = "default") -> TuningProfile:
    latest = _latest_by_name(conn, user_id)
    payload = latest.get(name)
    if payload is None:
        available = ", ".join(sorted(latest)) or "(none seeded)"
        raise KeyError(f"unknown tuning profile '{name}'; available: {available}")
    return TuningProfile.model_validate(payload)
