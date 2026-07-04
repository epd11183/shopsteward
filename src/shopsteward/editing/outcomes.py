"""Scan bridge done/failed result files and translate them into
editjob.completed/failed events. Idempotent by job_id, falling back to
file_name for results that lack one (malformed jobs keyed by filename)."""

import sqlite3

from shopsteward.adapters.lightroom.interface import LightroomBridge
from shopsteward.core.events import Event, append, read_all

_OUTCOME_EVENT_TYPES = ("editjob.completed", "editjob.failed")


def _known_outcome_keys(conn: sqlite3.Connection, user_id: int) -> set[str]:
    known: set[str] = set()
    for e in read_all(conn, "editjob."):
        if e.user_id != user_id or e.type not in _OUTCOME_EVENT_TYPES:
            continue
        for key in (e.payload.get("edit_job_id"), e.payload.get("file_name")):
            if key is not None:
                known.add(key)
    return known


def scan_outcomes(conn: sqlite3.Connection, user_id: int, bridge: LightroomBridge) -> int:
    known = _known_outcome_keys(conn, user_id)
    new_events = 0

    for payload in bridge.poll_results():
        job_id = payload.get("job_id")
        key = job_id if job_id is not None else payload.get("file_name")
        if key is not None and key in known:
            continue

        if payload.get("status") == "completed":
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="editjob.completed",
                    payload={
                        "edit_job_id": job_id,
                        "applied": payload.get("applied", 0),
                        "skipped": payload.get("skipped", []),
                        "exported": payload.get("exported", []),
                        "finished_at": payload.get("finished_at"),
                    },
                ),
            )
        else:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="editjob.failed",
                    payload={
                        "edit_job_id": job_id,
                        "file_name": payload.get("file_name"),
                        "error": payload.get("error", {}),
                    },
                ),
            )

        new_events += 1
        if key is not None:
            known.add(key)

    return new_events
