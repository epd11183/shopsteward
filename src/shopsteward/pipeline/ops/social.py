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


def mark_posted(
    conn: sqlite3.Connection,
    user_id: int,
    action_id: str,
    *,
    posted_event_type: str,
    resolve_drafted: Callable[[sqlite3.Connection, int, str], Event],
) -> bool:
    """Returns True if a new `posted_event_type` event was appended, False
    if `action_id` was already marked posted (safe no-op). `resolve_drafted`
    is responsible for raising `ValueError` (with its own message) when
    `action_id` doesn't resolve to a real drafted event of its channel --
    this function never silently no-ops on an unknown id."""
    drafted = resolve_drafted(conn, user_id, action_id)

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
                "posted_at": datetime.now(UTC).isoformat(),
            },
        ),
    )
    return True
