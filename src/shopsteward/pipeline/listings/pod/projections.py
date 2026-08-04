"""Derived read model for pod.json: proj_pod_config. Drop-and-rebuild, own
schema, own rebuild entrypoint (rebuild_pod_config) -- mirrors
editing/projections.py, mockups/projections.py, listings/projections.py.

This is a SEPARATE entrypoint from listings/projections.py's
rebuild_listings(): it folds a different event namespace (podconfig.* vs
listingconfig.*/listingdraft.*) into a different table (proj_pod_config vs
proj_listing_config/proj_listing_drafts), with no shared state either way.
rebuild_listings() does not call this, and this does not call
rebuild_listings() -- a pod-aware caller (pod/build.py, slice 2+) must
invoke both explicitly. See the matching note atop listings/projections.py.
"""

import json
import sqlite3

from shopsteward.core.events import read_all
from shopsteward.pipeline.listings.pod.config import POD_CONFIG_EVENT_TYPES

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_pod_config;
CREATE TABLE proj_pod_config (
    user_id INTEGER NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
"""


def rebuild_pod_config(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)
    for e in read_all(conn, "podconfig."):
        # Guard against a future event in the same namespace this fold
        # doesn't know how to handle yet (design §15's rollback lever
        # invites exactly that: a podconfig.disabled event) -- seed()
        # already applies this same filter.
        if e.type not in POD_CONFIG_EVENT_TYPES:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO proj_pod_config VALUES (?,?,?)",
            (e.user_id, e.payload["name"], json.dumps(e.payload["config"])),
        )
    conn.commit()
