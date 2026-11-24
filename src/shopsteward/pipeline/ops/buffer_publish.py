"""Buffer publish helper for `social.pinterest_post` Variant B (design
2026-11-22-buffer-social-adapter.md §4/§5, slice S5).

The never-create-twice contract. `createPost` has no idempotency key and a
refused action is not retried by `ops run`, so:
1. `social.buffer_queued` for the action_id is checked FIRST (a re-run returns
   the recorded result, never a second create).
2. Everything that can refuse (channel, board, image, slot, queue depth) is
   resolved BEFORE the intent marker is appended, so a refusal there leaves no
   blocking marker.
3. The drafted event (`publish_via: "buffer"`, with the action_id) is appended
   BEFORE `create_post`. `events.append` commits immediately, so the marker
   survives any later exception.
4. A timeout (`BufferUnknownOutcome`) triggers one inline reconcile. Found ->
   continue; not found -> raise a plain ValueError (non-terminal refusal) and
   leave the intent UNRESOLVED. An unresolved intent removes its listing from
   `pinterest_post._candidates` whatever the cooldown says.
5. `reconcile()` (run by `buffer-sync`) resolves intents: exactly one match ->
   `social.buffer_queued{reconciled: true}`; zero matches after the reconcile
   window, on a channel that THIS run actually listed -> `social.buffer_lost`
   (releases the pair). Ambiguity never creates and never releases.

Every raise here is a plain ValueError (never StaleTargetError): the runner
records a non-terminal `action.refused`, and an unresolved intent is exactly
the kind of temporary condition that must not burn the action_id."""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Protocol

from shopsteward.adapters.buffer.interface import (
    BufferAdapter,
    BufferUnknownOutcome,
    BufferWriteAdapter,
)
from shopsteward.adapters.buffer.models import BufferChannel, BufferPost, BufferPostSpec
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction
from shopsteward.pipeline.ops.timeutil import parse_ts

QUEUED = "social.buffer_queued"
DELETED = "social.buffer_deleted"
LOST = "social.buffer_lost"
PIN_DRAFTED = "social.pin_drafted"
CAPTION_DRAFTED = "social.caption_drafted"
_UTM = re.compile(r"utm_content=([0-9a-f]{12})")
_RECONCILE_LOOKBACK = timedelta(minutes=5)
_SLOT_HORIZON_DAYS = 60


class BufferPublisherAdapter(BufferAdapter, BufferWriteAdapter, Protocol):
    """Read + write: execute needs channels/posts as well as create/delete."""


@dataclass(frozen=True)
class Intent:
    event: Event

    @property
    def action_id(self) -> str:
        return self.event.payload["action_id"]

    @property
    def kind(self) -> str:
        return "caption" if self.event.type == CAPTION_DRAFTED else "pin"

    @property
    def exact_text(self) -> str | None:
        """A caption's own text, for channels whose post carries no UTM link."""
        return self.event.payload.get("caption") if self.kind == "caption" else None

    @property
    def drafted_at(self) -> datetime:
        return parse_ts(self.event.payload.get("drafted_at") or self.event.created_at)


def find_queued(conn: sqlite3.Connection, user_id: int, action_id: str) -> Event | None:
    return next(
        (
            e
            for e in read_all(conn, QUEUED)
            if e.user_id == user_id and e.payload.get("action_id") == action_id
        ),
        None,
    )


def unresolved_intents(conn: sqlite3.Connection, user_id: int) -> list[Intent]:
    """Buffer-path drafted events with neither `buffer_queued` nor `buffer_lost`."""
    resolved = {
        e.payload.get("action_id")
        for e in read_all(conn, "social.buffer_")
        if e.user_id == user_id and e.type in (QUEUED, LOST)
    }
    return [
        Intent(e)
        for t in (PIN_DRAFTED, CAPTION_DRAFTED)
        for e in read_all(conn, t)
        if e.user_id == user_id
        and e.payload.get("publish_via") == "buffer"
        and e.payload.get("action_id") not in resolved
    ]


def blocked_listings(conn: sqlite3.Connection, user_id: int) -> set[int]:
    """Listings with an unresolved PIN intent (captions block per channel, below)."""
    return {
        i.event.payload["listing_id"] for i in unresolved_intents(conn, user_id) if i.kind == "pin"
    }


def blocked_captions(conn: sqlite3.Connection, user_id: int) -> set[tuple[int, str]]:
    """(listing_id, channel) pairs with an unresolved caption intent."""
    return {
        (i.event.payload["listing_id"], i.event.payload["channel"])
        for i in unresolved_intents(conn, user_id)
        if i.kind == "caption"
    }


def match_posts(
    posts: list[BufferPost], action_id: str, exact_text: str | None = None
) -> list[BufferPost]:
    """Posts whose pin_url/text carries `utm_content == action_id[:12]`, or (a
    caption with no link, i.e. Instagram) whose text equals `exact_text`."""
    prefix = action_id[:12]
    return [
        p
        for p in posts
        if prefix in _UTM.findall(f"{p.pin_url or ''} {p.text}")
        or (exact_text is not None and p.text == exact_text)
    ]


def _find_post(
    adapter: BufferAdapter,
    channel_id: str,
    action_id: str,
    drafted_at: datetime,
    exact_text: str | None = None,
) -> BufferPost | None:
    posts = adapter.list_posts(
        channel_ids=[channel_id], created_since=drafted_at - _RECONCILE_LOOKBACK
    )
    hits = match_posts(posts, action_id, exact_text)
    return hits[0] if len(hits) == 1 else None  # ambiguous == not found: never create


def _queued_event(
    conn: sqlite3.Connection,
    user_id: int,
    intent_payload: dict,
    post: BufferPost,
    *,
    kind: str,
    reconciled: bool,
) -> Event:
    return append(
        conn,
        Event(
            user_id=user_id,
            type=QUEUED,
            payload={
                "action_id": intent_payload["action_id"],
                "listing_id": intent_payload["listing_id"],
                "kind": kind,
                "buffer_post_id": post.post_id,
                "channel_id": intent_payload["channel_id"],
                "channel": intent_payload["channel"],
                "due_at": intent_payload["due_at"],
                "ai_label_applied": False,
                "reconciled": reconciled,
            },
        ),
    )


def reconcile(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: BufferAdapter,
    cfg: OpsConfig,
    channels: list[BufferChannel],
    *,
    now: datetime,
) -> int:
    """Resolve unresolved intents (module docstring, point 5). Returns the
    number resolved. Only channels in `channels` (listed this run) can be
    judged lost -- an empty/offline adapter must never release a pair."""
    listed = {c.channel_id for c in channels}
    window = timedelta(minutes=cfg.buffer.reconcile_window_minutes)
    resolved = 0
    for intent in unresolved_intents(conn, user_id):
        p = intent.event.payload
        if p["channel_id"] not in listed:
            continue
        posts = adapter.list_posts(
            channel_ids=[p["channel_id"]], created_since=intent.drafted_at - _RECONCILE_LOOKBACK
        )
        hits = match_posts(posts, intent.action_id, intent.exact_text)
        if len(hits) == 1:
            _queued_event(conn, user_id, p, hits[0], kind=intent.kind, reconciled=True)
            resolved += 1
        elif not hits and now - intent.drafted_at > window:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type=LOST,
                    payload={"action_id": intent.action_id, "listing_id": p["listing_id"]},
                ),
            )
            resolved += 1
    return resolved


def _next_due(slots: list[str], earliest: datetime, taken: set[datetime]) -> datetime:
    for day in range(_SLOT_HORIZON_DAYS):
        for slot in sorted(slots):
            hh, mm = slot.split(":")
            cand = datetime.combine(
                earliest.date() + timedelta(days=day), time(int(hh), int(mm)), tzinfo=UTC
            )
            if cand >= earliest and cand not in taken:
                return cand
    raise ValueError("no free Buffer post slot within the horizon")


def result_of(queued: Event) -> ExecutionResult:
    p = queued.payload
    return ExecutionResult(
        before={},
        after={
            "listing_id": p["listing_id"],
            "buffer_post_id": p["buffer_post_id"],
            "channel": p["channel"],
            "due_at": p["due_at"],
        },
        cost_usd=0.0,
        duration_ms=0,
    )


def publish(
    conn: sqlite3.Connection,
    user_id: int,
    action: ProposedAction,
    publisher: BufferPublisherAdapter,
    cfg: OpsConfig,
    *,
    kind: str,  # "pin" | "caption"
    service: str,  # key into cfg.buffer.channels
    listing_id: int,
    drafted_fields: dict,  # the capability's own copy record (+ join keys)
    text: str,
    image_url: str,
    board_name: str | None = None,  # pinterest only
    pin_title: str | None = None,
    pin_url: str | None = None,
    now: datetime | None = None,
) -> ExecutionResult:
    """Steps 2-4 of the module docstring (step 1, the queued check, and the
    eligibility revalidation are done by the caller, in that order)."""
    now = now or datetime.now(UTC)
    ch_cfg = cfg.buffer.channels.get(service)
    if ch_cfg is None:
        raise ValueError(f"no buffer.channels.{service} config -- refusing to publish")
    mine = [c for c in publisher.list_channels() if c.service == ch_cfg.service]
    if len(mine) != 1:
        raise ValueError(f"expected exactly one {ch_cfg.service} channel, found {len(mine)}")
    channel = mine[0]
    if channel.is_disconnected or channel.is_locked or channel.is_queue_paused:
        raise ValueError(f"{ch_cfg.service} channel is disconnected, locked or paused")
    board_id = None
    if board_name is not None:
        board_id = channel.boards.get(board_name)
        if board_id is None:
            raise ValueError(f"board {board_name!r} not found on the {ch_cfg.service} channel")

    scheduled = publisher.list_posts(
        channel_ids=[channel.channel_id],
        created_since=now - timedelta(days=cfg.buffer.sync_lookback_days),
        statuses=["scheduled"],
    )
    if len(scheduled) >= ch_cfg.max_queue_depth:
        raise ValueError(f"Buffer queue is full for the {ch_cfg.service} channel")
    taken = {parse_ts(p.due_at.isoformat()) for p in scheduled if p.due_at}
    taken |= {
        parse_ts(e.payload["due_at"])
        for e in read_all(conn, QUEUED)
        if e.user_id == user_id and e.payload.get("channel_id") == channel.channel_id
    }
    due_at = _next_due(
        ch_cfg.post_times_utc, now + timedelta(hours=cfg.buffer.min_lead_hours), taken
    )

    intent_payload = {
        "action_id": action.action_id,
        "publish_via": "buffer",
        "listing_id": listing_id,
        **drafted_fields,
        "image_url": image_url,
        "channel_id": channel.channel_id,
        "channel": ch_cfg.service,
        "due_at": due_at.isoformat(),
        "drafted_at": now.isoformat(),
    }
    drafted_type = CAPTION_DRAFTED if kind == "caption" else PIN_DRAFTED
    append(conn, Event(user_id=user_id, type=drafted_type, payload=intent_payload))

    spec = BufferPostSpec(
        channel_id=channel.channel_id,
        text=text,
        image_url=image_url,
        mode="customScheduled",
        due_at=due_at,
        scheduling_type="automatic",
        pin_title=pin_title,
        pin_url=pin_url,
        pin_board_service_id=board_id,
        ai_assisted=True,  # true of the copy; isAiGenerated stays false (operator Q1)
    )
    try:
        post = publisher.create_post(spec)
    except BufferUnknownOutcome as exc:
        exact = drafted_fields.get("caption") if kind == "caption" else None
        found = _find_post(publisher, channel.channel_id, action.action_id, now, exact)
        if found is None:
            raise ValueError(
                f"Buffer create outcome unknown and the post was not found; intent left "
                f"unresolved for buffer-sync to reconcile ({exc})"
            ) from exc
        post = found
    queued = _queued_event(conn, user_id, intent_payload, post, kind=kind, reconciled=False)
    return result_of(queued)


def undo(
    conn: sqlite3.Connection,
    user_id: int,
    action: ProposedAction,
    publisher: BufferPublisherAdapter,
    cfg: OpsConfig,
    *,
    platform: str,
) -> None:
    """deletePost only while the post is still queued; after send Buffer's
    delete would not remove it from the network, so refuse clearly."""
    queued = find_queued(conn, user_id, action.action_id)
    if queued is None:
        raise ValueError(f"action {action.action_id}: no Buffer post recorded to undo")
    p = queued.payload
    posts = publisher.list_posts(
        channel_ids=[p["channel_id"]],
        created_since=datetime.now(UTC) - timedelta(days=cfg.buffer.sync_lookback_days),
    )
    post = next((x for x in posts if x.post_id == p["buffer_post_id"]), None)
    if post is None or post.status != "scheduled":
        state = "not found" if post is None else post.status
        raise ValueError(
            f"Buffer post is {state}, not queued -- remove it on {platform} directly "
            "(deleting in Buffer does not remove a sent post)"
        )
    publisher.delete_post(post.post_id)
    append(
        conn,
        Event(
            user_id=user_id,
            type=DELETED,
            payload={"action_id": action.action_id, "buffer_post_id": post.post_id},
        ),
    )
