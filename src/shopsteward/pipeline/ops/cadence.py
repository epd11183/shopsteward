"""Per-channel Buffer cadence, derived from events only (design
2026-11-22-buffer-social-adapter.md §4 governor / §7 facts, slice S7).

Counts posts from ANY source on a channel: the latest `buffer.post.observed`
per post (S4 -- includes the Desktop job's foreign posts, `action_id: null`)
plus local `social.buffer_queued` whose post is not observed yet. A post that
is in both is counted once (keyed by Buffer post id). A locally undone post
(`social.buffer_deleted`) is not counted.

Week bucketing: a `sent` post counts in the ISO week of `sent_at`, a
`scheduled` one in the ISO week of `due_at`. Other statuses (error, draft)
never count. Channels are keyed by the config's `buffer.channels` key and
matched to posts by that channel's `service`."""

import sqlite3
from datetime import date

from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import buffer_publish
from shopsteward.pipeline.ops.models import OpsConfig
from shopsteward.pipeline.ops.timeutil import parse_ts

_OBSERVED = "buffer.post.observed"
_COUNTED = ("sent", "scheduled")


def latest_observed(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    """post_id -> payload of the most recent `buffer.post.observed`."""
    out: dict[str, dict] = {}
    for e in read_all(conn, _OBSERVED):
        if e.user_id == user_id:
            out[str(e.payload.get("post_id"))] = e.payload
    return out


def _posts(conn: sqlite3.Connection, user_id: int) -> list[tuple[str, str, str | None]]:
    """(service, status, when-iso) per distinct live post, any source."""
    observed = latest_observed(conn, user_id)
    # A deleted (undone) post vanishes from Buffer, so its last `scheduled`
    # observation is never superseded: drop it from observed counts too.
    deleted = {
        e.payload.get("buffer_post_id")
        for e in read_all(conn, buffer_publish.DELETED)
        if e.user_id == user_id
    }
    posts = [
        (
            str(p.get("channel")),
            str(p.get("status")),
            (p.get("sent_at") if p.get("status") == "sent" else p.get("due_at")),
        )
        for pid, p in observed.items()
        if pid not in deleted
    ]
    for e in read_all(conn, buffer_publish.QUEUED):
        pid = e.payload.get("buffer_post_id")
        if e.user_id == user_id and pid not in observed and pid not in deleted:
            posts.append((str(e.payload["channel"]), "scheduled", e.payload.get("due_at")))
    return posts


def channel_load(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, today: date
) -> dict[str, tuple[int, int]]:
    """channel key -> (posts in today's ISO week, scheduled posts not yet due)."""
    iso = today.isocalendar()[:2]
    posts = _posts(conn, user_id)
    out: dict[str, tuple[int, int]] = {}
    for key, ch in cfg.buffer.channels.items():
        week = queued = 0
        for service, status, when in posts:
            if service != ch.service or status not in _COUNTED or not when:
                continue
            day = parse_ts(when).date()
            if day.isocalendar()[:2] == iso:
                week += 1
            if status == "scheduled" and day >= today:
                queued += 1
        out[key] = (week, queued)
    return out


def cadence_deficit(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, today: date
) -> dict[str, int]:
    """channel key -> posts still owed this ISO week (never negative)."""
    return {
        key: max(0, cfg.buffer.channels[key].posts_per_week - week)
        for key, (week, _q) in channel_load(conn, user_id, cfg, today).items()
    }


def pace_exceeded(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, today: date, channel: str
) -> bool:
    """True iff `channel` has met its weekly target or its queue is full.
    An unconfigured channel is not paced here (publish refuses it itself)."""
    ch = cfg.buffer.channels.get(channel)
    if ch is None:
        return False
    week, queued = channel_load(conn, user_id, cfg, today)[channel]
    return week >= ch.posts_per_week or queued >= ch.max_queue_depth
