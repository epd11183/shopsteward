"""Shared "operator marked this draft posted" mechanism between
`social.pinterest_post` and `social.caption_draft` (T5+E5, 2026-08-25
owned-channel premise-gate -- /autoplan Decision Audit Trail #9). See
`capabilities/caption_draft.py`'s module docstring for the eligibility-
policy half of this generalization (a config field, not a hardcoded
per-capability decision); this module is the mark-posted half.

Both capabilities' entire lifecycle is drafted -> (operator posts by hand,
outside this system) -> marked posted; neither ever calls a live API.
`mark_posted()` is the ONE place the idempotent-append logic lives: look up
the drafted event via the caller-supplied `resolve_drafted` (which must
either return the matching Event or raise `ValueError` itself, with a
channel-specific message -- `social.pinterest_post`'s original 2026-08-24
"no drafted pin found"/"full 64-char id" wording is preserved verbatim by
keeping that validation in ITS OWN resolver rather than centralizing a
generic message here), then append `posted_event_type` unless this
`action_id` is already marked (a safe no-op, never a double-append).

`resolve_drafted` is the one pluggable seam because the two channels'
join-back-to-a-draft mechanisms are genuinely different, not just
differently named: Pinterest's `social.pin_drafted` event carries no
`action_id` field at all -- the destination URL's `utm_content` embeds
`action_id[:12]` instead, a deliberate join key for a future
outcome-projection reader (pinterest_post.py's own module docstring) --
while a caption has no destination URL or any other place to embed a join
key, so it simply stores its own full `action_id` directly on the drafted
event and resolves by an exact match. Everything else -- the idempotent
check, the appended event's shape (`listing_id`/`action_id`/`posted_at`) --
is identical, which is what actually generalizes here."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.timeutil import parse_ts


def effective_at(e: Event) -> str | None:
    """E4 (2026-08-25, `--posted-at`): an event's own `posted_at` payload
    field if present (an operator may backdate this to when they actually
    posted, not when they ran `ops mark-posted`), else its append-time
    `created_at`. A `*_drafted` event never carries `posted_at`, so this
    always falls through to `created_at` for one -- safe to call on either
    a drafted or a posted event uniformly, everywhere recency of "when was
    this listing last pinned/captioned" is computed (cooldowns, the
    pin-experiment outcome readout)."""
    posted_at = e.payload.get("posted_at")
    return posted_at if isinstance(posted_at, str) else e.created_at


def mark_posted(
    conn: sqlite3.Connection,
    user_id: int,
    action_id: str,
    *,
    posted_event_type: str,
    resolve_drafted: Callable[[sqlite3.Connection, int, str], Event],
    posted_at: str | None = None,
) -> bool:
    """Returns True if a new `posted_event_type` event was appended, False
    if `action_id` was already marked posted (safe no-op). `resolve_drafted`
    is responsible for raising `ValueError` (with its own message) when
    `action_id` doesn't resolve to a real drafted event of its channel --
    this function never silently no-ops on an unknown id.

    `posted_at` (E4, 2026-08-25): an optional operator-supplied backdate for
    when the draft was ACTUALLY posted, distinct from "now" (when this CLI
    call runs). Validated to fall between the draft's own `drafted_at` and
    now, INCLUSIVE of both ends -- raises `ValueError` (never appends) on a
    future date or one before the draft even existed. `None` (the default)
    preserves today's exact behavior: stamp wall-clock now."""
    drafted = resolve_drafted(conn, user_id, action_id)

    now = datetime.now(UTC)
    if posted_at is None:
        posted_at_value = now.isoformat()
    else:
        posted_dt = parse_ts(posted_at)
        if posted_dt > now:
            raise ValueError(f"--posted-at {posted_at!r} is in the future")
        drafted_at_raw = drafted.payload.get("drafted_at") or drafted.created_at
        if drafted_at_raw is not None and posted_dt < parse_ts(drafted_at_raw):
            raise ValueError(
                f"--posted-at {posted_at!r} is before this draft's own drafted_at "
                f"{drafted_at_raw!r}"
            )
        posted_at_value = posted_dt.isoformat()

    already_posted = any(
        e.user_id == user_id and e.payload.get("action_id") == action_id
        for e in read_all(conn, posted_event_type)
    )
    if already_posted:
        return False

    append(
        conn,
        Event(
            user_id=user_id,
            type=posted_event_type,
            payload={
                "listing_id": drafted.payload["listing_id"],
                "action_id": action_id,
                "posted_at": posted_at_value,
            },
        ),
    )
    return True
