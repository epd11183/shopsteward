"""E12 -- proves the event-sourcing guarantee CLAUDE.md asserts but nothing
else tests: "Events are immutable and append-only; derived state is rebuilt
via projections." Specifically, for the ops action projection
(pipeline/ops/projections.py's action_rows()/rebuild_ops()):

1. replaying the SAME event log twice produces byte-for-byte identical
   proj_actions/proj_capability_state rows (test_projections.py already
   covers this for proj_listing_daily's idempotency; this file is the
   proj_actions/proj_capability_state analogue, PLUS the cross-database
   audit property those existing tests don't touch).
2. replaying into a completely fresh, independent database from a copy of
   the same log produces the SAME state -- the actual audit property: same
   log in, same state out, regardless of *where* or *when* you rebuild.
3. a well-formed log is never wall-clock-sensitive: _fold_capability_states
   (projections.py:257) has a `datetime.now(UTC)` fallback for a falsy
   `created_at`, which is the exact risk shape a guardrail review called
   out (a staleness/terminality decision buried in a fold making rebuilds
   non-deterministic). For every event this repo's own `core.events.append()`
   ever produces, `created_at` is never falsy (db.py's schema sets it
   NOT NULL with a DEFAULT), so that branch is provably dead for a
   real log -- proven here by making `datetime.now()` raise during a
   rebuild and asserting it never fires.
4. the events table itself is untouched by any of the above rebuilds, and
   rejects UPDATE/DELETE outright (db.py's triggers), which is the actual
   backstop against a fold ever mutating the source of truth.
"""

import sqlite3
from datetime import datetime

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.ops import projections
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops

USER_ID = 1
CAPABILITY = "ops.stub_lifecycle"

# Every action.* terminal (and non-terminal "proposed"/"approved") state
# action_rows() knows about, per its own docstring.
ALL_ROW_STATES = {
    "proposed",
    "approved",
    "executed",
    "refused",
    "rejected",
    "undone",
    "failed",
    "expired",
    "superseded",
}


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _proposed_payload(action_id: str, target_id: str, cost_usd: float = 1.5) -> dict:
    return {
        "action_id": action_id,
        "capability": CAPABILITY,
        "target_type": "listing",
        "target_id": target_id,
        "tier": int(Tier.PROPOSE),
        "reason": "replay-determinism fixture action.",
        "inputs_hash": f"hash-{action_id}",
        "estimated_cost_usd": cost_usd,
        "undo_available": True,
        "expires_at": "2099-12-31",
        "params": {},
    }


def seed_full_lifecycle(conn: sqlite3.Connection, user_id: int = USER_ID) -> None:
    """One action per state action_rows() can produce, plus a
    proposal_deduped event (which -- correctly -- yields no row, since it
    names a fresh action_id that was never itself proposed) and a
    capability.promoted/demoted pair, so proj_capability_state's fold is
    exercised too."""

    def prop(action_id: str, target_id: str) -> None:
        append(
            conn,
            Event(
                user_id=user_id,
                type="action.proposed",
                payload=_proposed_payload(action_id, target_id),
            ),
        )

    def resolve(kind: str, action_id: str, **extra: object) -> None:
        append(
            conn,
            Event(
                user_id=user_id, type=f"action.{kind}", payload={"action_id": action_id, **extra}
            ),
        )

    # -- proposed (still pending) --
    prop("a-proposed", "t-proposed")

    # -- approved --
    prop("a-approved", "t-approved")
    resolve("approved", "a-approved", by="operator")

    # -- executed --
    prop("a-executed", "t-executed")
    resolve("approved", "a-executed", by="operator")
    resolve("executed", "a-executed", before={"on": True}, after={"on": False}, cost_usd=1.5)

    # -- refused --
    prop("a-refused", "t-refused")
    resolve("refused", "a-refused", reason="policy_unverified")

    # -- rejected --
    prop("a-rejected", "t-rejected")
    resolve("rejected", "a-rejected")

    # -- undone (proposed -> approved -> executed -> undone) --
    prop("a-undone", "t-undone")
    resolve("approved", "a-undone", by="operator")
    resolve("executed", "a-undone", before={"on": True}, after={"on": False}, cost_usd=0.0)
    resolve("undone", "a-undone")

    # -- failed (proposed -> approved -> failed) --
    prop("a-failed", "t-failed")
    resolve("approved", "a-failed", by="operator")
    resolve("failed", "a-failed", error="capability raised during execute()")

    # -- expired --
    prop("a-expired", "t-expired")
    resolve("expired", "a-expired")

    # -- superseded (two siblings for the same target; the first is
    # superseded by the second, which stays "proposed") --
    prop("a-super-1", "t-super")
    prop("a-super-2", "t-super")
    resolve("superseded", "a-super-1", superseded_by="a-super-2")

    # -- proposal_deduped: names a brand-new action_id that was NEVER
    # itself proposed -- action_rows() must produce no row for it. --
    append(
        conn,
        Event(
            user_id=user_id,
            type="action.proposal_deduped",
            payload={
                "action_id": "a-deduped-new",
                "capability": CAPABILITY,
                "target_id": "t-approved",
                "existing_action_id": "a-approved",
            },
        ),
    )

    # -- capability ladder events, so proj_capability_state's fold is
    # exercised across a promote+demote pair too. --
    append(
        conn,
        Event(
            user_id=user_id,
            type="capability.promoted",
            payload={"capability": CAPABILITY, "to_tier": int(Tier.NOTIFY)},
        ),
    )
    append(
        conn,
        Event(
            user_id=user_id,
            type="capability.demoted",
            payload={"capability": CAPABILITY, "to_tier": int(Tier.PROPOSE)},
        ),
    )


def _action_rows(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM proj_actions ORDER BY user_id, action_id")
    return [dict(r) for r in cur.fetchall()]


def _capability_rows(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM proj_capability_state ORDER BY user_id, capability")
    return [dict(r) for r in cur.fetchall()]


def _event_rows(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT id, user_id, type, payload, created_at FROM events ORDER BY id")
    return [dict(r) for r in cur.fetchall()]


def _copy_events_preserving_order_and_timestamps(
    src: sqlite3.Connection, dst: sqlite3.Connection
) -> None:
    """A plain INSERT per event, in source id order, with the source's own
    created_at carried over explicitly (helpers.py's
    seed_listing_observed_on precedent for bypassing the DB-default
    timestamp) -- never an UPDATE/DELETE, so this is itself append-only."""
    for row in src.execute("SELECT user_id, type, payload, created_at FROM events ORDER BY id"):
        dst.execute(
            "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
            (row["user_id"], row["type"], row["payload"], row["created_at"]),
        )
    dst.commit()


# --- 1/2: replay determinism, same DB and a fresh, independent DB -----------


def test_action_projection_replay_is_byte_for_byte_deterministic(conn, tmp_path):
    seed_full_lifecycle(conn)

    rebuild_ops(conn)
    first_actions = _action_rows(conn)
    first_caps = _capability_rows(conn)

    assert {r["state"] for r in first_actions} == ALL_ROW_STATES
    assert len(first_actions) == 10  # proposed/approved/executed/refused/rejected/
    # undone/failed/expired + 2 siblings for superseded; deduped yields none.
    assert "a-deduped-new" not in {r["action_id"] for r in first_actions}

    # rebuild AGAIN in the same database -- must be pixel-identical.
    rebuild_ops(conn)
    second_actions = _action_rows(conn)
    second_caps = _capability_rows(conn)
    assert second_actions == first_actions
    assert second_caps == first_caps

    # rebuild into a SECOND, completely fresh database from a copy of the
    # SAME event log -- the real audit property: same log in, same state
    # out, regardless of where you rebuild.
    replica = connect(tmp_path / "replica.db")
    migrate(replica)
    _copy_events_preserving_order_and_timestamps(conn, replica)

    rebuild_ops(replica)
    replica_actions = _action_rows(replica)
    replica_caps = _capability_rows(replica)
    assert replica_actions == first_actions
    assert replica_caps == first_caps


# --- 3: order/wall-clock independence ----------------------------------------


def test_rebuild_never_consults_the_wall_clock_for_a_well_formed_log(conn, monkeypatch):
    """_fold_capability_states (projections.py:257) has a
    `datetime.now(UTC)` fallback for when an event's created_at is falsy --
    exactly the non-determinism risk shape a guardrail review called out.
    Every event this repo's append() ever writes has a real created_at
    (db.py's schema: NOT NULL DEFAULT strftime(...)), so that fallback must
    never fire for a real log. Proven by making datetime.now() raise during
    a rebuild of the full lifecycle fixture and asserting it doesn't."""
    seed_full_lifecycle(conn)
    rebuild_ops(conn)
    baseline_actions = _action_rows(conn)
    baseline_caps = _capability_rows(conn)

    class _NoWallClock(datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError(
                "rebuild_ops() consulted datetime.now() while replaying a "
                "fully-formed event log -- see projections.py:257's "
                "created_at fallback; a real log must never depend on "
                "wall-clock time at rebuild time"
            )

    monkeypatch.setattr(projections, "datetime", _NoWallClock)

    rebuild_ops(conn)  # must not raise

    assert _action_rows(conn) == baseline_actions
    assert _capability_rows(conn) == baseline_caps


# --- 4: the log itself is never mutated --------------------------------------


def test_events_table_is_unchanged_by_any_rebuild(conn):
    seed_full_lifecycle(conn)
    before = _event_rows(conn)

    rebuild_ops(conn)
    rebuild_ops(conn)

    after = _event_rows(conn)
    assert after == before


def test_events_table_rejects_update_and_delete_at_the_db_level(conn):
    """db.py's events_no_update/events_no_delete triggers are the actual
    backstop behind "never UPDATE or DELETE an event row" -- a projection
    bug that tried to mutate the log would hit this, not silently succeed."""
    seed_full_lifecycle(conn)
    (any_id,) = conn.execute("SELECT id FROM events LIMIT 1").fetchone()

    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET payload = '{}' WHERE id = ?", (any_id,))

    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events WHERE id = ?", (any_id,))
