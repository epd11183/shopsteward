"""Gate 1 queue read, decisions (approve|reject|snooze|requeue), and best-effort
undo/recall. Approve calls editing.dispatch.dispatch_edit_job(mode="hero") --
the only place pipeline crosses into editing (see pipeline/__init__.py)."""

import sqlite3
from pathlib import Path

from shopsteward.adapters.lightroom.interface import LightroomBridge
from shopsteward.core.events import Event, append
from shopsteward.editing.config import load_editing_defaults
from shopsteward.editing.dispatch import dispatch_edit_job
from shopsteward.pipeline import tuning
from shopsteward.pipeline.models import Gate1Card
from shopsteward.pipeline.projections import rebuild_pipeline

_DECISION_EVENT_TYPES = {
    "approve": "gate1.approved",
    "reject": "gate1.rejected",
    "snooze": "gate1.snoozed",
}

_QUERY_BY_STATE = """
SELECT g.photo_id, g.state, g.composite, g.edit_job_id,
       p.base_name,
       s.technical, s.commercial, s.escalated, s.subject,
       s.strongest_room_style, s.one_risk, s.rationale,
       ej.status AS dispatch_state
FROM proj_gate1 g
JOIN proj_photos p ON p.user_id = g.user_id AND p.photo_id = g.photo_id
LEFT JOIN proj_scores s ON s.user_id = g.user_id AND s.photo_id = g.photo_id
LEFT JOIN proj_edit_jobs ej ON ej.user_id = g.user_id AND ej.edit_job_id = g.edit_job_id
WHERE g.user_id = ? AND g.state = ?
ORDER BY g.composite DESC
"""

_QUERY_BY_PHOTO_ID = """
SELECT g.photo_id, g.state, g.composite, g.edit_job_id,
       p.base_name,
       s.technical, s.commercial, s.escalated, s.subject,
       s.strongest_room_style, s.one_risk, s.rationale,
       ej.status AS dispatch_state
FROM proj_gate1 g
JOIN proj_photos p ON p.user_id = g.user_id AND p.photo_id = g.photo_id
LEFT JOIN proj_scores s ON s.user_id = g.user_id AND s.photo_id = g.photo_id
LEFT JOIN proj_edit_jobs ej ON ej.user_id = g.user_id AND ej.edit_job_id = g.edit_job_id
WHERE g.user_id = ? AND g.photo_id = ?
ORDER BY g.composite DESC
"""


def _row_to_card(row: sqlite3.Row) -> Gate1Card:
    return Gate1Card(
        photo_id=row["photo_id"],
        base_name=row["base_name"],
        composite=row["composite"],
        technical=row["technical"],
        commercial=row["commercial"],
        subject=row["subject"] or "",
        strongest_room_style=row["strongest_room_style"] or "",
        one_risk=row["one_risk"] or "",
        rationale=row["rationale"] or "",
        escalated=bool(row["escalated"]) if row["escalated"] is not None else False,
        state=row["state"],
        edit_job_id=row["edit_job_id"],
        dispatch_state=row["dispatch_state"],
    )


def get_queue(conn: sqlite3.Connection, user_id: int, state: str = "pending") -> list[Gate1Card]:
    rows = conn.execute(_QUERY_BY_STATE, (user_id, state)).fetchall()
    return [_row_to_card(r) for r in rows]


def get_card(conn: sqlite3.Connection, user_id: int, photo_id: str) -> Gate1Card | None:
    rows = conn.execute(_QUERY_BY_PHOTO_ID, (user_id, photo_id)).fetchall()
    return _row_to_card(rows[0]) if rows else None


def _gate1_state(conn: sqlite3.Connection, user_id: int, photo_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM proj_gate1 WHERE user_id = ? AND photo_id = ?",
        (user_id, photo_id),
    ).fetchone()
    return row["state"] if row is not None else None


def decide(
    conn: sqlite3.Connection,
    user_id: int,
    bridge: LightroomBridge,
    photo_id: str,
    decision: str,
) -> Gate1Card:
    rebuild_pipeline(conn)  # ensure proj_gate1 exists even on a fresh DB
    state = _gate1_state(conn, user_id, photo_id)
    if state is None:
        raise ValueError(f"photo {photo_id!r} is not in the Gate 1 queue")

    if decision == "requeue":
        if state != "snoozed":
            raise ValueError(f"cannot requeue photo {photo_id!r} from state {state!r}")
        append(
            conn,
            Event(
                user_id=user_id,
                type="gate1.undone",
                payload={"photo_id": photo_id, "undo_of": "snoozed", "job_recalled": False},
            ),
        )
    elif decision in _DECISION_EVENT_TYPES:
        if state != "pending":
            raise ValueError(f"cannot {decision} photo {photo_id!r} from state {state!r}")
        if decision == "approve":
            _approve(conn, user_id, bridge, photo_id)
        else:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type=_DECISION_EVENT_TYPES[decision],
                    payload={"photo_id": photo_id},
                ),
            )
    else:
        raise ValueError(f"invalid decision {decision!r}")

    rebuild_pipeline(conn)
    card = get_card(conn, user_id, photo_id)
    if card is None:  # pragma: no cover - defensive, projection always has the row
        raise RuntimeError(f"gate1 card for {photo_id!r} disappeared after decide")
    return card


def _approve(
    conn: sqlite3.Connection, user_id: int, bridge: LightroomBridge, photo_id: str
) -> None:
    profile = tuning.get_profile(conn, user_id)
    job = dispatch_edit_job(
        conn,
        user_id,
        bridge,
        photo_ids=[photo_id],
        preset_family=profile.scoring.hero_preset_family,
        mode="hero",
        event_name=None,
        output_folder=None,
        editing_defaults=load_editing_defaults(),
    )
    row = conn.execute(
        "SELECT composite FROM proj_gate1 WHERE user_id = ? AND photo_id = ?",
        (user_id, photo_id),
    ).fetchone()
    append(
        conn,
        Event(
            user_id=user_id,
            type="gate1.approved",
            payload={
                "photo_id": photo_id,
                "composite": row["composite"],
                "edit_job_id": job.job_id,
            },
        ),
    )


def undo(conn: sqlite3.Connection, user_id: int, bridge: LightroomBridge, photo_id: str) -> dict:
    rebuild_pipeline(conn)  # ensure proj_gate1 exists even on a fresh DB
    row = conn.execute(
        "SELECT state, edit_job_id FROM proj_gate1 WHERE user_id = ? AND photo_id = ?",
        (user_id, photo_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"photo {photo_id!r} is not in the Gate 1 queue")

    state = row["state"]
    if state not in ("approved", "rejected", "snoozed"):
        raise ValueError(f"photo {photo_id!r} has no decision to undo (state={state!r})")

    job_recalled = False
    if state == "approved" and row["edit_job_id"]:
        # Best-effort recall: only removes the job if the Lua consumer hasn't
        # picked it up yet (still sitting in the jobs/ inbox, not moved to
        # done/ or failed/). Never touches done/ or failed/.
        job_file = Path(bridge.root) / "jobs" / f"edit_{row['edit_job_id']}.json"  # type: ignore[attr-defined]
        if job_file.exists():
            job_file.unlink()
            job_recalled = True

    append(
        conn,
        Event(
            user_id=user_id,
            type="gate1.undone",
            payload={"photo_id": photo_id, "undo_of": state, "job_recalled": job_recalled},
        ),
    )
    rebuild_pipeline(conn)
    return {"photo_id": photo_id, "undo_of": state, "job_recalled": job_recalled}
