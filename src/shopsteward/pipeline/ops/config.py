"""Path to shippable ops defaults + loader/hasher + event-seed/read-back --
pipeline/listings/pod/config.py precedent, including its apply() verb
(pod/config.py's docstring: apply() exists because seed() alone is
write-once and every threshold in ops.json -- windows, dead-listing
thresholds, product-type keywords, brief section toggles -- must be
editable by the operator without a migration).

seed() appends opsconfig.seeded once per user; get_ops_config() reads the
last-write-wins row from proj_ops_config (ops/projections.py's
rebuild_ops() must have run first). apply() re-reads the file and appends
opsconfig.updated only when its hash actually changed -- an unchanged file
is a no-op, so it is safe to call on every `ops config apply` invocation.
A caller must always hash the OpsConfig object it actually reads
(get_ops_config()'s return value), never re-derive the hash from the file
while other code reads the DB -- the two can silently diverge the moment
the file is edited without a matching apply()."""

import hashlib
import json
import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.models import OpsConfig

_REPO_ROOT = Path(__file__).resolve().parents[4]
OPS_CONFIG_PATH = _REPO_ROOT / "config" / "defaults" / "ops.json"

OPS_CONFIG_EVENT_TYPES = ("opsconfig.seeded", "opsconfig.updated")


def load_ops_config(path: Path = OPS_CONFIG_PATH) -> OpsConfig:
    return OpsConfig.model_validate_json(Path(path).read_text())


def ops_config_hash(cfg: OpsConfig) -> str:
    canonical = json.dumps(cfg.model_dump(by_alias=True), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def seed(conn: sqlite3.Connection, user_id: int, path: Path = OPS_CONFIG_PATH) -> bool:
    cfg = load_ops_config(path)

    # Read straight from events (not the projection table, which may not
    # have been rebuilt yet) -- pod/config.py precedent. Re-seeding on
    # changed defaults would silently revert a future operator
    # opsconfig.updated (last-write-wins), so once a name exists this is a
    # no-op.
    seeded_names = {
        e.payload["name"]
        for e in read_all(conn, "opsconfig.")
        if e.user_id == user_id and e.type in OPS_CONFIG_EVENT_TYPES
    }
    if cfg.name in seeded_names:
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="opsconfig.seeded",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "defaults",
            },
        ),
    )
    return True


def apply(conn: sqlite3.Connection, user_id: int, path: Path = OPS_CONFIG_PATH) -> bool:
    """Re-read `path`; if nothing has been seeded yet for this config name,
    seed it. Otherwise append opsconfig.updated only when the file's
    ops_config_hash differs from the last seeded/updated config for that
    name -- an unchanged file is a no-op. Caller must rebuild_ops()
    afterwards; get_ops_config() reads the projection, not events."""
    cfg = load_ops_config(path)

    last_config: dict | None = None
    for e in read_all(conn, "opsconfig."):
        if (
            e.user_id == user_id
            and e.type in OPS_CONFIG_EVENT_TYPES
            and e.payload["name"] == cfg.name
        ):
            last_config = e.payload["config"]

    if last_config is None:
        return seed(conn, user_id, path)

    if ops_config_hash(cfg) == ops_config_hash(OpsConfig.model_validate(last_config)):
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="opsconfig.updated",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "operator",
            },
        ),
    )
    return True


def get_ops_config(conn: sqlite3.Connection, user_id: int, name: str = "default") -> OpsConfig:
    row = conn.execute(
        "SELECT config_json FROM proj_ops_config WHERE user_id=? AND name=?", (user_id, name)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown ops config '{name}' for user {user_id}")
    return OpsConfig.model_validate(json.loads(row["config_json"]))
