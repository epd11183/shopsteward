"""Path to shippable listing defaults + loader/hasher (mockups/config.py
precedent) + event-seed/read-back (tuning.py precedent): seed() appends
listingconfig.seeded once per user, get_config() reads the last-write-wins
row from proj_listing_config (rebuild_listings() must have run)."""

import hashlib
import json
import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.models import ListingConfig

_REPO_ROOT = Path(__file__).resolve().parents[4]
LISTING_CONFIG_PATH = _REPO_ROOT / "config" / "defaults" / "listing.json"

LISTING_CONFIG_EVENT_TYPES = ("listingconfig.seeded", "listingconfig.updated")


def load_listing_config(path: Path = LISTING_CONFIG_PATH) -> ListingConfig:
    return ListingConfig.model_validate_json(Path(path).read_text())


def config_hash(cfg: ListingConfig) -> str:
    canonical = json.dumps(cfg.model_dump(by_alias=True), sort_keys=True, separators=(",", ":"))
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
