"""Editing-local, event-sourced cost ledger for look generation. Cannot use
pipeline.llm_ledger (import-linter), so it appends `llm.call` events to the core
log and sums them by month. The month prefix (YYYY-MM) is supplied by the caller
to keep this library free of wall-clock reads."""

import sqlite3

from shopsteward.adapters.look.interface import LookUsage
from shopsteward.core.events import Event, append, read_all

_LOOK_LLM_TYPE = "llm.call"


def append_llm_call(
    conn: sqlite3.Connection, user_id: int, usage: LookUsage, *, description: str
) -> None:
    append(
        conn,
        Event(
            user_id=user_id,
            type=_LOOK_LLM_TYPE,
            payload={
                "feature": "look",
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "est_cost_usd": usage.est_cost_usd,
                "description": description,
            },
        ),
    )


def month_look_cost(conn: sqlite3.Connection, user_id: int, month_prefix: str) -> float:
    total = 0.0
    for e in read_all(conn, _LOOK_LLM_TYPE):
        if (
            e.user_id == user_id
            and e.payload.get("feature") == "look"
            and (e.created_at or "").startswith(month_prefix)
        ):
            total += e.payload.get("est_cost_usd") or 0.0
    return total
