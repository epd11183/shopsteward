"""Path to shippable listing defaults + loader/hasher (mockups/config.py
precedent) + event-seed/read-back (tuning.py precedent): seed() appends
listingconfig.seeded once per user, get_config() reads the last-write-wins
row from proj_listing_config (rebuild_listings() must have run)."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.models import ListingConfig

_REPO_ROOT = Path(__file__).resolve().parents[4]
LISTING_CONFIG_PATH = _REPO_ROOT / "config" / "defaults" / "listing.json"

LISTING_CONFIG_EVENT_TYPES = ("listingconfig.seeded", "listingconfig.updated")


def load_listing_config(path: Path = LISTING_CONFIG_PATH) -> ListingConfig:
    # encoding="utf-8" explicit (M4, guardrail review 2026-08-25).
    return ListingConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


# digital_files is excluded from the hash UNCONDITIONALLY: draft_id embeds the
# hash, so including it would re-key every existing (pushed) draft and risk
# duplicate live listings. Slice 3 must not rely on config_hash for it.
_HASH_EXCLUDE = {"digital_files"}


_IDENTITY_KEYS = {"name", "schema"}


def config_hash(cfg: ListingConfig) -> str:
    canonical = json.dumps(
        cfg.model_dump(by_alias=True, exclude=_HASH_EXCLUDE), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def seed(conn: sqlite3.Connection, user_id: int, path: Path = LISTING_CONFIG_PATH) -> bool:
    cfg = load_listing_config(path)

    # Read straight from events (not the projection table, which may not
    # have been rebuilt yet) -- tuning.py precedent. Re-seeding on changed
    # defaults would silently revert a future operator listingconfig.updated
    # (last-write-wins), so once a name exists this is a no-op.
    seeded_names = {
        e.payload["name"]
        for e in read_all(conn, "listingconfig.")
        if e.user_id == user_id and e.type in LISTING_CONFIG_EVENT_TYPES
    }
    if cfg.name in seeded_names:
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingconfig.seeded",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "defaults",
            },
        ),
    )
    return True


def get_config(conn: sqlite3.Connection, user_id: int, name: str = "default") -> ListingConfig:
    row = conn.execute(
        "SELECT config_json FROM proj_listing_config WHERE user_id=? AND name=?", (user_id, name)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown listing config '{name}' for user {user_id}")
    return ListingConfig.model_validate(json.loads(row["config_json"]))


def _stored_raw(conn: sqlite3.Connection, user_id: int, name: str) -> dict[str, Any]:
    """Raw stored JSON (not re-dumped through the model, so keys a pre-slice-2
    seed never wrote are genuinely absent). Needs rebuild_listings() first."""
    row = conn.execute(
        "SELECT config_json FROM proj_listing_config WHERE user_id=? AND name=?", (user_id, name)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown listing config '{name}' for user {user_id}")
    return json.loads(row["config_json"])


def _append_updated(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: ListingConfig,
    source: str,
    config: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingconfig.updated",
            payload={
                "name": cfg.name,
                "config": config if config is not None else cfg.model_dump(by_alias=True),
                "source": source,
                **extra,
            },
        ),
    )


def sync_defaults(
    conn: sqlite3.Connection,
    user_id: int,
    path: Path = LISTING_CONFIG_PATH,
    name: str = "default",
) -> list[str]:
    """Merge only top-level keys MISSING from the stored config out of the
    defaults file; one listingconfig.updated event, none if nothing is
    missing. Returns the added keys. Never overwrites an existing value."""
    stored = _stored_raw(conn, user_id, name)
    defaults = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in defaults if k not in stored]
    if not missing:
        return []
    cfg = ListingConfig.model_validate({**stored, **{k: defaults[k] for k in missing}})
    _append_updated(conn, user_id, cfg, "sync-defaults", added=missing)
    return missing


def set_value(
    conn: sqlite3.Connection,
    user_id: int,
    dotted_key: str,
    value: Any,
    name: str = "default",
) -> ListingConfig:
    """Set ONE existing (possibly null-valued) key by dotted path, validate the
    whole result through ListingConfig, append listingconfig.updated. Raises
    ValueError (nothing appended) on an unknown path or invalid result."""
    stored = _stored_raw(conn, user_id, name)
    raw = ListingConfig.model_validate(stored).model_dump(by_alias=True)
    *parents, leaf = dotted_key.split(".")
    if (parents[0] if parents else leaf) in _IDENTITY_KEYS:
        raise ValueError(f"config identity key {dotted_key!r} cannot be changed")
    node: Any = raw
    for part in parents:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise ValueError(f"unknown or non-object config path: {dotted_key!r}")
    if not isinstance(node, dict) or leaf not in node:
        raise ValueError(f"unknown config key: {dotted_key!r}")
    node[leaf] = value
    try:
        cfg = ListingConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid config after setting {dotted_key!r}: {exc}") from exc
    # Keep keys the stored config never had absent (not null), so sync-defaults
    # can still fill them: only the key being set may newly appear.
    top = parents[0] if parents else leaf
    out = {k: v for k, v in cfg.model_dump(by_alias=True).items() if k in stored or k == top}
    _append_updated(conn, user_id, cfg, "set", config=out, key=dotted_key)
    return cfg
