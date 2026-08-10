"""Event-sourced look store (presets.py precedent) + described-look resolution.
Named looks seed from config/defaults/looks/*.json; described looks are keyed by
normalized description so an identical phrase reloads instead of regenerating."""

import hashlib
import json
import sqlite3
from pathlib import Path

from shopsteward.adapters.look.interface import LookAdapter, LookProfile
from shopsteward.core.events import Event, append, read_all

LOOK_EVENT_TYPES = ("look.seeded", "look.updated")


def _latest_by_name(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for e in read_all(conn, "look."):
        if e.user_id != user_id or e.type not in LOOK_EVENT_TYPES:
            continue
        latest[e.payload["name"]] = e.payload
    return latest


def _profile_from_payload(payload: dict) -> LookProfile:
    return LookProfile.model_validate(payload["profile"])


def seed(conn: sqlite3.Connection, user_id: int, defaults_dir: Path) -> int:
    existing = _latest_by_name(conn, user_id)
    seeded = 0
    for path in sorted(Path(defaults_dir).glob("*.json")):
        profile = LookProfile.model_validate(json.loads(path.read_text()))
        prior = existing.get(profile.name)
        if prior is not None and prior.get("profile") == profile.model_dump():
            continue
        append(conn, Event(user_id=user_id, type="look.seeded",
                           payload={"name": profile.name, "profile": profile.model_dump(),
                                    "source": "defaults"}))
        seeded += 1
    return seeded


def list_looks(conn: sqlite3.Connection, user_id: int) -> list[LookProfile]:
    return [_profile_from_payload(p) for _, p in sorted(_latest_by_name(conn, user_id).items())]


def get_look(conn: sqlite3.Connection, user_id: int, name: str) -> LookProfile:
    latest = _latest_by_name(conn, user_id)
    payload = latest.get(name)
    if payload is None:
        available = ", ".join(sorted(latest)) or "(none seeded)"
        raise KeyError(f"unknown look '{name}'; available: {available}")
    return _profile_from_payload(payload)


def save_look(conn: sqlite3.Connection, user_id: int, profile: LookProfile) -> None:
    append(conn, Event(user_id=user_id, type="look.updated",
                       payload={"name": profile.name, "profile": profile.model_dump(),
                                "source": "generated"}))


def _desc_key(description: str) -> str:
    normalized = " ".join(description.lower().split())
    return "desc:" + hashlib.sha256(normalized.encode()).hexdigest()[:12]


def resolve_look(
    conn: sqlite3.Connection,
    user_id: int,
    look_arg: str,
    adapter: LookAdapter,
    *,
    model: str,
    regenerate: bool,
) -> LookProfile:
    """Resolve --look: an exact stored name wins; otherwise treat as a description
    keyed by normalized text (reload unless --regenerate); else generate + save."""
    latest = _latest_by_name(conn, user_id)
    if look_arg in latest:
        return _profile_from_payload(latest[look_arg])

    key = _desc_key(look_arg)
    if not regenerate and key in latest:
        return _profile_from_payload(latest[key])

    result = adapter.generate_look(look_arg, model=model)
    profile = result.profile.model_copy(update={"name": key, "description": look_arg})
    save_look(conn, user_id, profile)
    return profile
