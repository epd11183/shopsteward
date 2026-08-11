"""LLM narration of the deterministic Brief (M8b slice 1, design §5/§6).

narrate_brief() is the only place that decides whether a narration call is
allowed: it reuses the shared `llm_ledger` monthly soft cap (the same pool
copy/vision spend against, PRD §13 decisions 22/38) rather than a
planner-specific cap. Zero new actions -- this only narrates the
already-rendered deterministic brief text; it proposes nothing."""

import logging
import sqlite3

import httpx

from shopsteward.adapters.planner.interface import PlannerAdapter, PlannerParseError
from shopsteward.core.events import Event, append
from shopsteward.pipeline.llm_ledger import monthly_spend

logger = logging.getLogger(__name__)


def narrate_brief(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: PlannerAdapter,
    brief_text: str,
    *,
    soft_cap_usd: float,
    model: str,
) -> str | None:
    """Returns the narration text, or None when narration is skipped (over
    the shared monthly cap) or fails (transport/parse error) -- the caller
    always still has the deterministic brief text to print either way."""
    if monthly_spend(conn, user_id) >= soft_cap_usd:
        logger.warning(
            "monthly llm.call soft cap reached (>= %.2f usd); skipping brief narration",
            soft_cap_usd,
        )
        return None

    try:
        narration = adapter.narrate(brief_text)
    except (PlannerParseError, httpx.HTTPError) as exc:
        logger.warning("brief narration unavailable: %s", type(exc).__name__)
        return None

    append(
        conn,
        Event(
            user_id=user_id,
            type="llm.call",
            payload={
                "producer": "ops.planner.narrate",
                "model": model,
                "est_cost_usd": narration.usage.est_cost_usd,
                "prompt_tokens": narration.usage.prompt_tokens,
                "completion_tokens": narration.usage.completion_tokens,
            },
        ),
    )
    return narration.text
