"""`ops buffer-sync` core (design 2026-11-22-buffer-social-adapter.md §6, slice S4).

Read-only against Buffer: appends `buffer.channels.observed` (only when the
channel set changed), `buffer.post.observed` (only when a post's status, send
time, metrics or mapped action changed -- so an unchanged re-sync appends
nothing), and, on a `sent` post that maps to a drafted pin/caption, the
existing `social.pin_posted` / `social.caption_posted` via the same
`mark_posted()` that `ops mark-posted` uses (`posted_at == sent_at`).

Mapping: `social.buffer_queued` (post_id -> action) is primary (S5; intents are
reconciled into it first, see `buffer_publish.reconcile`). The content join below
stays as the fallback for posts the Desktop job made:
- the pin/caption's `utm_content=<action_id[:12]>` found in the post's pin_url
  or text -> the executed `social.pinterest_post` / `social.caption_draft`
  action with that id prefix;
- else a drafted caption whose text equals the post text on the same service.
Foreign posts (the Desktop job's) are still recorded as observed, unmapped:
the §4 cadence governor must count posts from any source."""

import re
import sqlite3
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from shopsteward.adapters.buffer.interface import BufferAdapter
from shopsteward.adapters.buffer.models import BufferChannel, BufferPost
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops import buffer_publish
from shopsteward.pipeline.ops.config import get_ops_config
from shopsteward.pipeline.ops.projections import action_rows

_UTM = re.compile(r"utm_content=([0-9a-f]{12})")
_CHANNELS = "buffer.channels.observed"
_POST = "buffer.post.observed"


class SyncResult(BaseModel):
    seen: dict[str, int] = Field(default_factory=dict)  # "service/status" -> count
    observed_new: int = 0  # buffer.post.observed appended
    marked_posted: int = 0  # social.*_posted appended
    unmatched: int = 0  # posts with no drafted pin/caption behind them
    reconciled: int = 0  # unresolved publish intents resolved (queued or lost)
    skipped: list[str] = Field(default_factory=list)  # mapped but not markable


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _channels_payload(channels: list[BufferChannel]) -> dict:
    return {
        "channels": sorted(
            (
                {
                    "channel_id": c.channel_id,
                    "service": c.service,
                    "is_disconnected": c.is_disconnected,
                    "is_locked": c.is_locked,
                    "is_queue_paused": c.is_queue_paused,
                    "boards": sorted(c.boards),
                }
                for c in channels
            ),
            key=lambda c: c["channel_id"],
        )
    }


def _find_action(
    conn: sqlite3.Connection, user_id: int, post: BufferPost, service: str
) -> tuple[str, str] | None:
    """(kind 'pin'|'caption', full action_id) or None."""
    prefix_m = _UTM.search(post.pin_url or "") or _UTM.search(post.text)
    if prefix_m:
        for row in action_rows(conn):
            if (
                row["user_id"] == user_id
                and row["state"] == "executed"
                and row["action_id"].startswith(prefix_m.group(1))
            ):
                # The service must match: a pin's link reused in an IG/FB post
                # must never mark the pin posted (review 2026-11-23).
                if row["capability"] == "social.pinterest_post" and service == "pinterest":
                    return "pin", row["action_id"]
                if row["capability"] == "social.caption_draft" and service != "pinterest":
                    return "caption", row["action_id"]
    text = post.text.strip()
    if text:
        for e in read_all(conn, "social.caption_drafted"):
            p = e.payload
            if (
                e.user_id == user_id
                and p.get("channel") == service
                and str(p.get("caption", "")).strip() == text
            ):
                return "caption", p["action_id"]
    return None


def sync_buffer(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: BufferAdapter,
    *,
    since_days: int = 35,
    now: datetime | None = None,
) -> SyncResult:
    from shopsteward.pipeline.ops.capabilities import caption_draft, pinterest_post

    now = now or datetime.now(UTC)
    res = SyncResult()
    channels = adapter.list_channels()
    service_of = {c.channel_id: c.service for c in channels}

    ch_payload = _channels_payload(channels)
    prior = [e for e in read_all(conn, _CHANNELS) if e.user_id == user_id]
    if not prior or prior[-1].payload != ch_payload:
        append(conn, Event(user_id=user_id, type=_CHANNELS, payload=ch_payload))

    if buffer_publish.unresolved_intents(conn, user_id):
        res.reconciled = buffer_publish.reconcile(
            conn, user_id, adapter, get_ops_config(conn, user_id), channels, now=now
        )
    queued = {
        e.payload["buffer_post_id"]: (e.payload.get("kind", "pin"), e.payload["action_id"])
        for e in read_all(conn, buffer_publish.QUEUED)
        if e.user_id == user_id
    }

    posts = adapter.list_posts(
        channel_ids=list(service_of),
        created_since=now - timedelta(days=since_days),
        with_metrics=True,
    )
    observed: dict[str, dict] = {}
    for e in read_all(conn, _POST):
        if e.user_id == user_id:
            observed[str(e.payload.get("post_id"))] = e.payload

    for post in sorted(posts, key=lambda p: p.post_id):
        service = service_of.get(post.channel_id, "unknown")
        key = f"{service}/{post.status}"
        res.seen[key] = res.seen.get(key, 0) + 1

        mapped = queued.get(post.post_id) or _find_action(conn, user_id, post, service)
        state = {
            "post_id": post.post_id,
            "channel": service,
            "status": post.status,
            "due_at": _iso(post.due_at),
            "sent_at": _iso(post.sent_at),
            "metrics": dict(sorted(post.metrics.items())),  # absent != 0: never zero-filled
            "action_id": mapped[1] if mapped else None,
        }
        prev = observed.get(post.post_id)
        if prev is None or {k: prev.get(k) for k in state} != state:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type=_POST,
                    payload={**state, "metrics_updated_at": _iso(post.metrics_updated_at)},
                ),
            )
            res.observed_new += 1

        if mapped is None:
            res.unmatched += 1
            continue
        if post.status != "sent" or post.sent_at is None:
            continue
        kind, action_id = mapped
        mark = pinterest_post.mark_posted if kind == "pin" else caption_draft.mark_posted
        try:
            if mark(conn, user_id, action_id, posted_at=post.sent_at.isoformat()):
                res.marked_posted += 1
        except ValueError as exc:  # e.g. sent before the draft existed: record, never guess
            res.skipped.append(f"{post.post_id}: {exc}")
    return res
