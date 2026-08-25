"""Event-sourced look store (presets.py precedent) + described-look resolution.
Named looks seed from config/defaults/looks/*.json; described looks are keyed by
normalized description so an identical phrase reloads instead of regenerating."""

import hashlib
import json
import sqlite3
from pathlib import Path

from shopsteward.adapters.look.interface import LookAdapter, LookProfile
from shopsteward.core.events import Event, append, read_all
from shopsteward.editing.look_cost import append_llm_call, month_look_cost
from shopsteward.editing.look_guard import sanitize_look

LOOK_EVENT_TYPES = ("look.seeded", "look.updated")


class LookCostCapError(RuntimeError):
    """Raised when the month's look-LLM spend is already at/over the soft cap."""


def _latest_by_name(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for e in read_all(conn, "look."):
        if e.user_id != user_id or e.type not in LOOK_EVENT_TYPES:
            continue
        latest[e.payload["name"]] = e.payload
    return latest


def _profile_from_payload(payload: dict) -> LookProfile:
    return LookProfile.model_validate(payload["profile"])


def seed(conn: sqlite3.Connection, user_id: int, defaults_dir: Path) -> int:
    existing = _latest_by_name(conn, user_id)
    seeded = 0
    for path in sorted(Path(defaults_dir).glob("*.json")):
        # encoding="utf-8" explicit (M4, guardrail review 2026-08-25).
        profile = LookProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
        prior = existing.get(profile.name)
        if prior is not None and prior.get("profile") == profile.model_dump():
            continue
        append(
            conn,
            Event(
                user_id=user_id,
                type="look.seeded",
                payload={
                    "name": profile.name,
                    "profile": profile.model_dump(),
                    "source": "defaults",
                },
            ),
        )
        seeded += 1
    return seeded


def list_looks(conn: sqlite3.Connection, user_id: int) -> list[LookProfile]:
    return [_profile_from_payload(p) for _, p in sorted(_latest_by_name(conn, user_id).items())]


def get_look(conn: sqlite3.Connection, user_id: int, name: str) -> LookProfile:
    latest = _latest_by_name(conn, user_id)
    payload = latest.get(name)
    if payload is None:
        available = ", ".join(sorted(latest)) or "(none seeded)"
        raise KeyError(f"unknown look '{name}'; available: {available}")
    return _profile_from_payload(payload)


def save_look(conn: sqlite3.Connection, user_id: int, profile: LookProfile) -> None:
    append(
        conn,
        Event(
            user_id=user_id,
            type="look.updated",
            payload={"name": profile.name, "profile": profile.model_dump(), "source": "generated"},
        ),
    )


def _desc_key(description: str) -> str:
    normalized = " ".join(description.lower().split())
    return "desc:" + hashlib.sha256(normalized.encode()).hexdigest()[:12]


def resolve_look(
    conn: sqlite3.Connection,
    user_id: int,
    look_arg: str,
    adapter: LookAdapter,
    *,
    model: str,
    regenerate: bool,
    guard_knobs: dict | None = None,
    soft_cap_usd: float | None = None,
    fallback_look: str = "bright-and-true",
    month_prefix: str | None = None,
) -> LookProfile:
    """Resolve --look: an exact stored name wins; otherwise treat as a description
    keyed by normalized text (reload unless --regenerate); else generate + save.
    When generating: enforce the monthly soft cap, run the sanity guard
    (retry once, then fall back to a seed), and ledger the LLM cost."""
    latest = _latest_by_name(conn, user_id)
    if look_arg in latest:
        return _profile_from_payload(latest[look_arg])

    key = _desc_key(look_arg)
    if not regenerate and key in latest:
        return _profile_from_payload(latest[key])

    if soft_cap_usd is not None:
        if month_prefix is None:
            raise ValueError("soft_cap_usd requires month_prefix")
        # ponytail: cap is entry-gated, not per-call — a single resolve can spend up
        # to 2x one generation past the cap via the guard retry. Acceptable for a soft cap.
        if month_look_cost(conn, user_id, month_prefix) >= soft_cap_usd:
            raise LookCostCapError(
                f"look-LLM spend for {month_prefix} is at the ${soft_cap_usd} cap; "
                "raise look_llm.monthly_soft_cap_usd to continue"
            )

    profile = _generate_gated(
        conn, user_id, look_arg, key, adapter, model, guard_knobs, fallback_look
    )
    save_look(conn, user_id, profile)
    return profile


def _generate_gated(
    conn: sqlite3.Connection,
    user_id: int,
    look_arg: str,
    key: str,
    adapter: LookAdapter,
    model: str,
    guard_knobs: dict | None,
    fallback_look: str,
) -> LookProfile:
    for _attempt in range(2):  # generate, then one retry on a guard failure
        result = adapter.generate_look(look_arg, model=model)
        if result.usage is not None:
            append_llm_call(conn, user_id, result.usage, description=look_arg)
        candidate = result.profile.model_copy(update={"name": key, "description": look_arg})
        if guard_knobs is None or sanitize_look(candidate, guard_knobs).ok:
            return candidate
    # Both attempts tripped the guard. Cache the seed under this description's key
    # so repeats reload it cheaply; the operator can force a fresh try with --regenerate.
    seed = get_look(conn, user_id, fallback_look)
    return seed.model_copy(
        update={
            "name": key,
            "description": f"{look_arg} (fell back to {fallback_look})",
        }
    )
