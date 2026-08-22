"""Derived read models for M8a slice 1, rebuilt from the event log. Safe to
drop and rebuild (rebuild_ops()) -- editing/mockups/listings precedent: the
projection table lives in projections.py, not config.py.

proj_listing_daily is the whole point of this slice (design §0(a)/§7/§9):
core/projections.py's rebuild() folds etsy.listing.observed into
proj_listings with INSERT OR REPLACE keyed on listing_id alone, so one
day's sync overwrites the last -- the per-listing history is discarded.
Here the fold buckets on the EVENT's own created_at date and keys on
(user_id, listing_id, day), so N observations of one listing across N
distinct days produce N rows, and "last observation within a day wins"
only collapses same-day re-syncs.

proj_sale_items is the one new sales projection this slice adds. Design
§0(a)/§8.3 asks to reuse an existing sales/receipts projection rather than
duplicate one: core/projections.py's proj_sales already exists and is
reused as-is (rebuild_core() must be called separately -- see
pipeline/ops/cli.py) for receipt-level revenue-by-day. It has no
listing_id, though, so it cannot answer "which listings sold" or "revenue
by product type" -- the analytics this slice exists to produce. That
attribution is genuinely new, so proj_sale_items folds
etsy.sale.observed's transactions[] (each already carries listing_id,
per §0(a)) into one row per transaction. It does NOT re-derive receipt
totals -- analytics.py sums proj_sale_items only for listing-level
breakdowns and reads core's proj_sales for shop-wide revenue, so the two
are never a source of disagreement about total revenue.

proj_ops_config mirrors pod/projections.py's proj_pod_config exactly.

proj_actions and proj_capability_state (added in the PR1 chassis slice,
M8a spec §3/§8.1) fold the action.*/capability.* event stream. Both fold
functions are exposed as capability_states()/action_rows() so runner.py and
governor.py can read live ladder/action state without depending on
rebuild_ops() having just run -- they read straight from the event log,
llm_ledger.py precedent."""

import json
import sqlite3
from datetime import UTC, datetime

from shopsteward.core.events import read_all
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.ops.config import OPS_CONFIG_EVENT_TYPES
from shopsteward.pipeline.ops.models import CapabilityState, Tier

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_listing_daily;
CREATE TABLE proj_listing_daily (
    user_id INTEGER NOT NULL, listing_id INTEGER NOT NULL, day TEXT NOT NULL,
    views INTEGER NOT NULL, num_favorers INTEGER NOT NULL, price_usd REAL NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (user_id, listing_id, day)
);
DROP TABLE IF EXISTS proj_sale_items;
CREATE TABLE proj_sale_items (
    user_id INTEGER NOT NULL, transaction_id INTEGER NOT NULL, receipt_id INTEGER NOT NULL,
    listing_id INTEGER NOT NULL, quantity INTEGER NOT NULL, price_usd REAL NOT NULL,
    sale_date TEXT NOT NULL,
    PRIMARY KEY (user_id, transaction_id)
);
DROP TABLE IF EXISTS proj_ops_config;
CREATE TABLE proj_ops_config (
    user_id INTEGER NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
    PRIMARY KEY (user_id, name)
);
DROP TABLE IF EXISTS proj_actions;
CREATE TABLE proj_actions (
    user_id INTEGER NOT NULL, action_id TEXT NOT NULL, capability TEXT NOT NULL,
    target_type TEXT NOT NULL, target_id TEXT NOT NULL, tier INTEGER NOT NULL,
    state TEXT NOT NULL, reason TEXT NOT NULL, inputs_hash TEXT NOT NULL,
    cost_usd REAL NOT NULL, before_json TEXT, after_json TEXT,
    proposed_at TEXT, resolved_at TEXT,
    PRIMARY KEY (user_id, action_id)
);
DROP TABLE IF EXISTS proj_capability_state;
CREATE TABLE proj_capability_state (
    user_id INTEGER NOT NULL, capability TEXT NOT NULL, tier INTEGER NOT NULL,
    approvals INTEGER NOT NULL, rejections INTEGER NOT NULL, undos INTEGER NOT NULL,
    executions INTEGER NOT NULL, tier_since TEXT NOT NULL, last_action_at TEXT,
    PRIMARY KEY (user_id, capability)
);
"""


def action_rows(conn: sqlite3.Connection) -> list[dict]:
    """Fold every user's action.* events into proj_actions rows (state =
    latest terminal: proposed -> approved -> executed | refused | rejected
    | undone | failed). Pure fold over the event log -- callable before or
    after rebuild_ops() has run."""
    rows: dict[tuple[int, str], dict] = {}
    for e in read_all(conn, "action."):
        p = e.payload
        action_id = p.get("action_id")
        if action_id is None:
            continue
        key = (e.user_id, action_id)
        kind = e.type.split(".", 1)[1]
        if kind == "proposed":
            rows[key] = {
                "user_id": e.user_id,
                "action_id": action_id,
                "capability": p["capability"],
                "target_type": p["target_type"],
                "target_id": str(p["target_id"]),
                "tier": int(p["tier"]),
                "state": "proposed",
                "reason": p["reason"],
                "inputs_hash": p["inputs_hash"],
                "cost_usd": p.get("estimated_cost_usd", 0.0),
                "before_json": None,
                "after_json": None,
                "proposed_at": e.created_at,
                "resolved_at": None,
            }
            continue
        row = rows.get(key)
        if row is None:
            continue  # a resolution event for an action_id we never saw proposed -- ignore
        if kind == "approved":
            row["state"] = "approved"
        elif kind == "executed":
            row["state"] = "executed"
            row["before_json"] = json.dumps(p["before"])
            row["after_json"] = json.dumps(p["after"])
            row["cost_usd"] = p.get("cost_usd", row["cost_usd"])
            row["resolved_at"] = e.created_at
        elif kind == "refused":
            row["state"] = "refused"
            row["resolved_at"] = e.created_at
        elif kind == "rejected":
            row["state"] = "rejected"
            row["resolved_at"] = e.created_at
        elif kind == "undone":
            row["state"] = "undone"
            row["resolved_at"] = e.created_at
        elif kind == "failed":
            row["state"] = "failed"
            row["resolved_at"] = e.created_at
    return list(rows.values())


def _fold_capability_states(conn: sqlite3.Connection) -> dict[tuple[int, str], CapabilityState]:
    """A SINGLE event-ordered pass over action.*/capability.* together --
    fold order MUST match append order, or a capability.demoted's counter
    reset only zeroes out whatever the fold had accumulated *so far in the
    loop*, not "as of that event in real history" (two independent passes
    got this wrong: the capability.* pass ran strictly after the action.*
    pass had already summed every approval/rejection/undo/execution ever,
    so a demotion appeared to reset counters that had, in event-time,
    already been reset and re-accumulated)."""
    states: dict[tuple[int, str], CapabilityState] = {}
    action_capability: dict[tuple[int, str], str] = {}
    action_tier: dict[tuple[int, str], Tier] = {}

    def ensure(user_id: int, cap_key: str, day: str) -> CapabilityState:
        sk = (user_id, cap_key)
        if sk not in states:
            states[sk] = CapabilityState(capability=cap_key, tier=Tier.PROPOSE, tier_since=day)
        return states[sk]

    events = [e for e in read_all(conn) if e.type.startswith(("action.", "capability."))]
    for e in events:
        p = e.payload
        day = (e.created_at or "")[:10] or datetime.now(UTC).date().isoformat()

        if e.type.startswith("capability."):
            if e.type not in ("capability.promoted", "capability.demoted"):
                continue
            st = ensure(e.user_id, p["capability"], day)
            st.tier = Tier(int(p["to_tier"]))
            st.tier_since = day
            if e.type == "capability.demoted":
                st.approvals = 0
                st.rejections = 0
                st.undos = 0
                st.executions = 0
            continue

        action_id = p.get("action_id")
        if action_id is None:
            continue
        akey = (e.user_id, action_id)
        kind = e.type.split(".", 1)[1]
        if kind == "proposed":
            action_capability[akey] = p["capability"]
            action_tier[akey] = Tier(int(p["tier"]))
            ensure(e.user_id, p["capability"], day).last_action_at = e.created_at
            continue
        cap_key = action_capability.get(akey)
        if cap_key is None:
            continue
        st = ensure(e.user_id, cap_key, day)
        st.last_action_at = e.created_at
        if kind == "approved" and p.get("by") == "operator":
            st.approvals += 1
        elif kind == "executed" and action_tier.get(akey) == Tier.NOTIFY:
            st.executions += 1
        elif kind == "rejected":
            st.rejections += 1
        elif kind == "undone":
            st.undos += 1

    return states


def capability_states(conn: sqlite3.Connection, user_id: int) -> dict[str, CapabilityState]:
    """Public, per-user view of the ladder state -- reads straight from the
    event log (llm_ledger.monthly_spend() precedent), so runner.py/
    governor.py never need rebuild_ops() to have just run."""
    return {
        cap_key: state
        for (uid, cap_key), state in _fold_capability_states(conn).items()
        if uid == user_id
    }


def rebuild_ops(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)

    for e in read_live_observed(conn, "etsy.listing.observed"):
        if e.created_at is None:
            continue  # defensive -- the events table default always sets this
        day = e.created_at[:10]
        p = e.payload
        conn.execute(
            "INSERT OR REPLACE INTO proj_listing_daily VALUES (?,?,?,?,?,?,?)",
            (
                e.user_id,
                p["listing_id"],
                day,
                p.get("views", 0),
                p.get("num_favorers", 0),
                p["price"]["amount"] / p["price"]["divisor"],
                p["state"],
            ),
        )

    for e in read_live_observed(conn, "etsy.sale.observed"):
        p = e.payload
        sale_date = datetime.fromtimestamp(p["created_timestamp"], tz=UTC).date().isoformat()
        for t in p.get("transactions", []):
            conn.execute(
                "INSERT OR REPLACE INTO proj_sale_items VALUES (?,?,?,?,?,?,?)",
                (
                    e.user_id,
                    t["transaction_id"],
                    p["receipt_id"],
                    t["listing_id"],
                    t["quantity"],
                    t["price"]["amount"] / t["price"]["divisor"],
                    sale_date,
                ),
            )

    for e in read_all(conn, "opsconfig."):
        # Guard against a future event in the same namespace this fold
        # doesn't know how to handle yet (pod/projections.py precedent).
        if e.type not in OPS_CONFIG_EVENT_TYPES:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO proj_ops_config VALUES (?,?,?)",
            (e.user_id, e.payload["name"], json.dumps(e.payload["config"])),
        )

    for row in action_rows(conn):
        conn.execute(
            "INSERT OR REPLACE INTO proj_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["user_id"],
                row["action_id"],
                row["capability"],
                row["target_type"],
                row["target_id"],
                row["tier"],
                row["state"],
                row["reason"],
                row["inputs_hash"],
                row["cost_usd"],
                row["before_json"],
                row["after_json"],
                row["proposed_at"],
                row["resolved_at"],
            ),
        )

    for (uid, cap_key), st in _fold_capability_states(conn).items():
        conn.execute(
            "INSERT OR REPLACE INTO proj_capability_state VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uid,
                cap_key,
                int(st.tier),
                st.approvals,
                st.rejections,
                st.undos,
                st.executions,
                st.tier_since,
                st.last_action_at,
            ),
        )

    conn.commit()
