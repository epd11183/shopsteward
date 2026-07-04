"""Preset-family store: seed from config/defaults/preset_families/*.json into
the event log, then read back last-write-wins by name."""

import json
import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append, read_all
from shopsteward.editing.models import PresetFamily

PRESET_EVENT_TYPES = ("presetfamily.seeded", "presetfamily.updated")


def _latest_by_name(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for e in read_all(conn, "presetfamily."):
        if e.user_id != user_id or e.type not in PRESET_EVENT_TYPES:
            continue
        latest[e.payload["name"]] = e.payload
    return latest


def seed(conn: sqlite3.Connection, user_id: int, defaults_dir: Path) -> int:
    existing = _latest_by_name(conn, user_id)
    seeded_count = 0
    for path in sorted(Path(defaults_dir).glob("*.json")):
        family = PresetFamily.model_validate(json.loads(path.read_text()))
        prior = existing.get(family.name)
        if prior is not None and prior.get("settings") == family.settings:
            continue
        append(
            conn,
            Event(
                user_id=user_id,
                type="presetfamily.seeded",
                payload={
                    "name": family.name,
                    "description": family.description,
                    "settings": family.settings,
                    "source": "defaults",
                },
            ),
        )
        seeded_count += 1
    return seeded_count


def list_families(conn: sqlite3.Connection, user_id: int) -> list[PresetFamily]:
    return [
        PresetFamily(name=name, description=p.get("description", ""), settings=p["settings"])
        for name, p in sorted(_latest_by_name(conn, user_id).items())
    ]


def get_family(conn: sqlite3.Connection, user_id: int, name: str) -> PresetFamily:
    latest = _latest_by_name(conn, user_id)
    payload = latest.get(name)
    if payload is None:
        available = ", ".join(sorted(latest)) or "(none seeded)"
        raise KeyError(f"unknown preset family '{name}'; available: {available}")
    return PresetFamily(
        name=name, description=payload.get("description", ""), settings=payload["settings"]
    )
