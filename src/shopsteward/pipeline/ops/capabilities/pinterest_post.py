"""`social.pinterest_post` Variant A -- Claude drafts a Pinterest pin (title,
description, alt text, board) for an active listing; the operator pastes it
into Pinterest by hand (design `docs/designs/2026-08-24-pinterest-adapter-
and-loop-roadmap.md` §2, item 2 of the 2026-08-24 sign-off register).
**No Pinterest call, ever** -- `adapters/pinterest` stays unwired; this
capability's entire output is one `social.pin_drafted` event, the
`social.caption_draft` precedent (module docstring there) for an
unpublishable-by-code artifact.

T2 ceiling (`max_tier = Tier.PROPOSE`, matching `social.caption_draft`'s
current setting for consistency -- the design doc's Tier.NOTIFY
recommendation is for the not-yet-built live Variant B only, which holds a
real `PinterestWriteAdapter` and a real `delete_pin` undo; this draft-only
variant has zero execution risk but stays at the same operator-glance
ceiling as its sibling draft capability). `policy_verified = True`: drafting
marketing copy that is never published is the same approved LLM use as
`social.caption_draft`'s caption text -- nothing reaches Pinterest.
**`undo = None`** explicitly: allowed because max_tier is not below PROPOSE
(registry.py invariant 1), and there is nothing to reverse programmatically
-- the reversal is "don't post it".

Holds NO adapter -- `execute()` only ever appends an event.

**Eligibility deliberately does NOT gate on `analytics.top_sellers()`**
(design §2.1) -- a pin is free, individually deletable, one of dozens, and
a long-lived search-index entry rather than a one-off feed post, so the
correct policy is coverage-first (least-recently-pinned first), not
proof-first. `_candidates()` is the ONE grounding function shared by
materialize()/execute() (M8b slice-2 planner-safety contract): a listing is
a candidate iff (1) `proj_listings.state == "active"`, (2) a usable image
exists (`url_570xN` from the stored `etsy.listing.images.observed`
projection -- no live Etsy call), and (3) no `social.pin_posted` /
`social.pin_drafted` event for that listing_id within
`cfg.pinterest.cooldown_days`.

**The destination URL is computed deterministically by this module, never
by the LLM** -- `utm_content` is `action_id[:12]`, the join key a future
outcome-projection reader will use (design §2.2/P1). The LLM supplies only
`title`, `description`, `alt_text`, `board_key`; `board_key` must be a key
in the config-declared `cfg.pinterest.boards` map so the LLM can never
invent a board. Validation drops, never truncates -- the same rule
`caption_draft._valid_caption`/`seo_edit._validate_params` use."""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.ops.config import get_ops_config, ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id

_PIN_EVENT_TYPES = ("social.pin_drafted", "social.pin_posted")


@dataclass(frozen=True)
class _Target:
    listing_id: int
    title: str
    image_url: str


def _image_url(conn: sqlite3.Connection, user_id: int, listing_id: int) -> str | None:
    """The most recent `etsy.listing.images.observed` payload for this
    listing (autorenew.py/seo_edit.py's `_latest_observed` precedent) --
    prefers the rank-1 image, falls back to the first image with a
    `url_570xN`. Returns None if no usable image has ever been observed."""
    images: list[dict] | None = None
    for e in read_live_observed(conn, "etsy.listing.images.observed"):
        if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
            continue
        images = e.payload.get("images") or []
    if not images:
        return None
    by_rank = sorted(
        (img for img in images if img.get("url_570xN")), key=lambda img: img.get("rank", 1)
    )
    return by_rank[0]["url_570xN"] if by_rank else None


def _last_pinned_at(conn: sqlite3.Connection, user_id: int, listing_id: int) -> str | None:
    """Most recent `social.pin_drafted`/`social.pin_posted` created_at for
    this listing, or None if it has never been pinned -- the cooldown +
    least-recently-pinned-first grounding."""
    latest: str | None = None
    for event_type in _PIN_EVENT_TYPES:
        for e in read_all(conn, event_type):
            if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
                continue
            if e.created_at and (latest is None or e.created_at > latest):
                latest = e.created_at
    return latest


def _candidates(conn: sqlite3.Connection, user_id: int, cfg: OpsConfig) -> dict[str, _Target]:
    """target_id (`str(listing_id)`) -> pin-eligible facts -- the ONE
    grounding function shared by materialize()/execute() so a hallucinated,
    inactive, imageless, or recently-pinned target is always dropped, never
    guessed."""
    out: dict[str, _Target] = {}
    rows = conn.execute(
        "SELECT listing_id, title FROM proj_listings WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall()
    cutoff = (datetime.now(UTC) - timedelta(days=cfg.pinterest.cooldown_days)).isoformat()
    for r in rows:
        image_url = _image_url(conn, user_id, r["listing_id"])
        if image_url is None:
            continue
        last_pinned = _last_pinned_at(conn, user_id, r["listing_id"])
        if last_pinned is not None and last_pinned >= cutoff:
            continue
        out[str(r["listing_id"])] = _Target(
            listing_id=r["listing_id"], title=r["title"], image_url=image_url
        )
    return out


def _destination_url(listing_id: int, action_id: str) -> str:
    return (
        f"https://www.etsy.com/listing/{listing_id}"
        f"?utm_source=pinterest&utm_medium=social&utm_campaign=shopsteward"
        f"&utm_content={action_id[:12]}"
    )


def _valid_params(params: object, cfg: OpsConfig) -> dict[str, str] | None:
    """Structural validation only (drop, never truncate/clamp) -- non-empty
    `str` within each field's configured limit, plus `board_key` must be a
    key in `cfg.pinterest.boards` (the LLM can never invent a board)."""
    if not isinstance(params, dict):
        return None

    def _valid_str(value: object, max_len: int) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        if len(value) > max_len:
            return None
        return value

    title = _valid_str(params.get("title"), cfg.pinterest.max_title_len)
    description = _valid_str(params.get("description"), cfg.pinterest.max_description_len)
    alt_text = _valid_str(params.get("alt_text"), cfg.pinterest.max_alt_text_len)
    board_key = params.get("board_key")

    if title is None or description is None or alt_text is None:
        return None
    if not isinstance(board_key, str) or board_key not in cfg.pinterest.boards:
        return None

    return {
        "title": title,
        "description": description,
        "alt_text": alt_text,
        "board_key": board_key,
    }


def _build_action(
    target: _Target, valid: dict[str, str], cfg_hash: str, today: str, expires_at: str
) -> ProposedAction:
    raw = "|".join(
        (str(target.listing_id), hashlib.sha256(repr(sorted(valid.items())).encode()).hexdigest())
    )
    inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
    action_id = compute_action_id(
        "social.pinterest_post", str(target.listing_id), inputs_hash, cfg_hash, today
    )
    destination_url = _destination_url(target.listing_id, action_id)
    return ProposedAction(
        action_id=action_id,
        capability="social.pinterest_post",
        target_type="listing",
        target_id=str(target.listing_id),
        tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
        reason=f"{target.title} -- not pinned within the cooldown window, ready to draft.",
        inputs_hash=inputs_hash,
        estimated_cost_usd=0.0,
        undo_available=False,
        expires_at=expires_at,
        params={**valid, "destination_url": destination_url, "image_url": target.image_url},
    )


class SocialPinterestPost:
    key = "social.pinterest_post"
    # T2 ceiling, matching social.caption_draft's current setting for
    # consistency (module docstring) -- Variant B's Tier.NOTIFY
    # recommendation applies only to the not-yet-built live variant.
    max_tier = Tier.PROPOSE
    policy_verified = True  # draft-only, never published -- see module docstring.
    # No undo path: allowed because max_tier is not below PROPOSE
    # (registry.py invariant 1) -- the reversal is "don't post it".
    undo = None

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return []  # planner-only -- no sensible deterministic pin copy.

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        target = _candidates(conn, user_id, cfg).get(intent.target_id)
        if target is None:
            return None  # ungrounded (hallucinated, inactive, imageless, or cooling down)

        valid = _valid_params(intent.params, cfg)
        if valid is None:
            return None  # invalid or unknown board -- dropped, never clamped

        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
        return _build_action(target, valid, cfg_hash, today, expires_at)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        cfg = get_ops_config(conn, user_id)
        target = _candidates(conn, user_id, cfg).get(action.target_id)
        if target is None:
            raise ValueError(f"listing {listing_id}: no longer pin-eligible -- refusing draft")

        valid = _valid_params(action.params, cfg)
        if valid is None:
            raise ValueError(
                f"action {action.action_id}: params are no longer valid -- refusing draft"
            )

        # Always recomputed, never read from action.params -- the LLM must
        # never be able to steer the destination URL (review finding).
        destination_url = _destination_url(listing_id, action.action_id)

        append(
            conn,
            Event(
                user_id=user_id,
                type="social.pin_drafted",
                payload={
                    "listing_id": listing_id,
                    "title": valid["title"],
                    "description": valid["description"],
                    "alt_text": valid["alt_text"],
                    "board_key": valid["board_key"],
                    "destination_url": destination_url,
                    "image_url": target.image_url,
                    "drafted_at": datetime.now(UTC).isoformat(),
                },
            ),
        )
        return ExecutionResult(
            before={},
            after={"listing_id": listing_id, "board_key": valid["board_key"]},
            cost_usd=0.0,
            duration_ms=0,
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
