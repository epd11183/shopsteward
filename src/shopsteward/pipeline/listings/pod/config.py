"""Path to shippable pod defaults + loader/hasher (listings/config.py
precedent) + event-seed/read-back: seed() appends podconfig.seeded once per
user, get_pod_config() reads the last-write-wins row from proj_pod_config
(pod/projections.py's rebuild_pod_config() must have run first). pod.json is
a SEPARATE config file from listing.json with its own hash -- folding the
two would change listings/config.py's config_hash() and orphan every
existing digital draft_id (design §2). The proj_pod_config schema and its
rebuild live in pod/projections.py (editing/mockups/listings precedent: the
projection table lives in projections.py, not config.py).

apply() (review fix-up C) is the rollback path seed() alone never provided:
seed() short-circuits forever once a name has been seeded, and nothing else
emitted podconfig.updated (it existed only in the event-type tuple and a
comment), so design §6's `enabled:false` kill switch, its price remedy
("change pod.json markup and rebuild"), and §14's "populate real base
costs" smoke-test step were all silent no-ops. apply() re-reads the file
and appends podconfig.updated only when its hash actually changed --
last-write-wins in rebuild_pod_config() picks it up from there. A caller
must always hash the PodConfig object it actually reads (get_pod_config()'s
return value), never re-derive the hash from the file while other code
reads the DB -- the two can silently diverge the moment the file is edited
without a matching apply()."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.pod.models import PodConfig

_REPO_ROOT = Path(__file__).resolve().parents[5]
POD_CONFIG_PATH = _REPO_ROOT / "config" / "defaults" / "pod.json"

POD_CONFIG_EVENT_TYPES = ("podconfig.seeded", "podconfig.updated")


def resolve_store_id(cfg: PodConfig) -> str:
    """Gelato store id from the GELATO_STORE_ID env var (name declared by
    cfg.catalog["gelato"].store_id_env), falling back to cfg.gelato.store_id.
    The committed pod.json holds only a placeholder there -- this PUBLIC repo
    never carries a real store id -- so the operator sets GELATO_STORE_ID and
    the placeholder fallback fails fast at PodProviderRef validation."""
    catalog = cfg.catalog.get("gelato")
    env_name = catalog.store_id_env if catalog else "GELATO_STORE_ID"
    return os.environ.get(env_name) or cfg.gelato.store_id


def load_pod_config(path: Path = POD_CONFIG_PATH) -> PodConfig:
    # encoding="utf-8" explicit (M4, guardrail review 2026-08-25).
    return PodConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


def pod_config_hash(cfg: PodConfig) -> str:
    canonical = json.dumps(cfg.model_dump(by_alias=True), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def seed(conn: sqlite3.Connection, user_id: int, path: Path = POD_CONFIG_PATH) -> bool:
    cfg = load_pod_config(path)

    # Read straight from events (not the projection table, which may not
    # have been rebuilt yet) -- listingconfig.py precedent. Re-seeding on
    # changed defaults would silently revert a future operator
    # podconfig.updated (last-write-wins), so once a name exists this is a
    # no-op.
    seeded_names = {
        e.payload["name"]
        for e in read_all(conn, "podconfig.")
        if e.user_id == user_id and e.type in POD_CONFIG_EVENT_TYPES
    }
    if cfg.name in seeded_names:
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="podconfig.seeded",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "defaults",
            },
        ),
    )
    return True


def apply(conn: sqlite3.Connection, user_id: int, path: Path = POD_CONFIG_PATH) -> bool:
    """Re-read `path`; if nothing has been seeded yet for this config name,
    seed it. Otherwise append podconfig.updated only when the file's
    pod_config_hash differs from the last seeded/updated config for that
    name -- an unchanged file is a no-op, so this is safe to call on every
    `pod config apply` invocation. Caller must rebuild_pod_config()
    afterwards; get_pod_config() reads the projection, not events."""
    cfg = load_pod_config(path)

    last_config: dict | None = None
    for e in read_all(conn, "podconfig."):
        if (
            e.user_id == user_id
            and e.type in POD_CONFIG_EVENT_TYPES
            and e.payload["name"] == cfg.name
        ):
            last_config = e.payload["config"]

    if last_config is None:
        return seed(conn, user_id, path)

    if pod_config_hash(cfg) == pod_config_hash(PodConfig.model_validate(last_config)):
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type="podconfig.updated",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "operator",
            },
        ),
    )
    return True


def get_pod_config(conn: sqlite3.Connection, user_id: int, name: str = "default") -> PodConfig:
    row = conn.execute(
        "SELECT config_json FROM proj_pod_config WHERE user_id=? AND name=?", (user_id, name)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown pod config '{name}' for user {user_id}")
    return PodConfig.model_validate(json.loads(row["config_json"]))
