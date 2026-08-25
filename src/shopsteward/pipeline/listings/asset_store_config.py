"""Path to shippable asset-store defaults + loader/hash/seed/apply/get
(listings/config.py, pod/config.py precedent) -- config for the managed
local archive that lets a future reprint resolve a photo's original master
after the landing folder is cleared (design: 2026-08-11-source-asset-head).
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.models import AssetStoreConfig

_REPO_ROOT = Path(__file__).resolve().parents[4]
ASSET_STORE_CONFIG_PATH = _REPO_ROOT / "config" / "defaults" / "asset_store.json"

ASSET_STORE_CONFIG_EVENT_TYPES = ("assetstoreconfig.seeded", "assetstoreconfig.updated")


def load_asset_store_config(path: Path = ASSET_STORE_CONFIG_PATH) -> AssetStoreConfig:
    # encoding="utf-8" explicit (M4, guardrail review 2026-08-25).
    return AssetStoreConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


def asset_store_config_hash(cfg: AssetStoreConfig) -> str:
    canonical = json.dumps(cfg.model_dump(by_alias=True), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def seed(conn: sqlite3.Connection, user_id: int, path: Path = ASSET_STORE_CONFIG_PATH) -> bool:
    cfg = load_asset_store_config(path)

    # Read straight from events (not the projection table, which may not
    # have been rebuilt yet) -- listings/config.py precedent. Re-seeding on
    # changed defaults would silently revert a future operator
    # assetstoreconfig.updated (last-write-wins), so once a name exists this
    # is a no-op.
    seeded_names = {
        e.payload["name"]
        for e in read_all(conn, "assetstoreconfig.")
        if e.user_id == user_id and e.type in ASSET_STORE_CONFIG_EVENT_TYPES
    }
    if cfg.name in seeded_names:
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="assetstoreconfig.seeded",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "defaults",
            },
        ),
    )
    return True


def apply(conn: sqlite3.Connection, user_id: int, path: Path = ASSET_STORE_CONFIG_PATH) -> bool:
    """Re-read `path`; seed if nothing has been seeded yet for this config
    name, else append assetstoreconfig.updated only when the file's hash
    differs from the last seeded/updated config for that name (pod/config.py
    apply() precedent) -- tests use this to point `root` at a tmp dir."""
    cfg = load_asset_store_config(path)

    last_config: dict | None = None
    for e in read_all(conn, "assetstoreconfig."):
        if (
            e.user_id == user_id
            and e.type in ASSET_STORE_CONFIG_EVENT_TYPES
            and e.payload["name"] == cfg.name
        ):
            last_config = e.payload["config"]

    if last_config is None:
        return seed(conn, user_id, path)

    if asset_store_config_hash(cfg) == asset_store_config_hash(
        AssetStoreConfig.model_validate(last_config)
    ):
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="assetstoreconfig.updated",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "operator",
            },
        ),
    )
    return True


def get_asset_store_config(
    conn: sqlite3.Connection, user_id: int, name: str = "default"
) -> AssetStoreConfig:
    row = conn.execute(
        "SELECT config_json FROM proj_asset_store_config WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown asset store config '{name}' for user {user_id}")
    return AssetStoreConfig.model_validate(json.loads(row["config_json"]))


def resolve_root(cfg: AssetStoreConfig) -> Path:
    """Archive root, resolved relative to the repo root like other default
    paths. `cfg.root` is CONFIG so tests point it at a tmp dir -- an already
    -absolute `cfg.root` passes through unchanged (pathlib's `/` yields just
    the right-hand operand when it is absolute), so a test override never
    touches the real `data/`."""
    return _REPO_ROOT / cfg.root
