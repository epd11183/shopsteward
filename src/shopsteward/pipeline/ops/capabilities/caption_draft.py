"""`social.caption_draft` -- Claude writes a promo social caption for a
listing; the operator copy-pastes it to a named CHANNEL (instagram/
facebook) and posts MANUALLY (M8b slice 6, design §10 CMO / draft §3.3
#26; T5+E5, 2026-08-25 owned-channel premise-gate: /autoplan CEO Decision
#2 -- ~75% of this shop's historical sales came from the operator's
personal IG/FB network, which the original plan omitted). **No Meta/IG/FB
call, ever** -- IG/FB auto-publish is blocked on Meta App Review +
Business Verification (`docs/policy/2026-08-11-autonomy-platform-policy.md`);
`adapters/meta` stays unwired. This capability's entire output is one
`social.caption_drafted` event; the reversal is trivial -- the operator
simply never posts it.

T2 ceiling, never auto/promotable (`max_tier = Tier.PROPOSE`, registry.py's
invariant 2): marketing copy addressed to the shop's audience is worth an
operator glance regardless of the ladder. `policy_verified = True` (writing
marketing copy is the same approved LLM use as `listing.seo_edit`'s title/
tags -- nothing is ever published). **`undo = None`** explicitly: allowed
because max_tier is not below PROPOSE (registry.py invariant 1), and there
is nothing to reverse programmatically -- the reversal is "don't post it"
(`listing.gapfill_reprint`'s own precedent for an unpublishable artifact).

Holds NO adapter -- unlike the Etsy-write capabilities this module never
constructs or is given one; `execute()` only ever appends an event.

Planner-only, like `listing.seo_edit`: `propose()` always returns `[]` --
writing a good caption is exactly the "deterministic heuristic too blunt"
case (design §11.1); there is no sensible deterministic caption to
generate. `_candidates()` is the grounding function shared by propose()/
materialize() (M8b slice-2 planner-safety contract), across EVERY
configured channel.

**`execute()`'s own re-validation is narrower than `_candidates()`, by
design (H2a, guardrail review 2026-08-25).** `_candidates()` folds together
two different kinds of question: genuine per-target staleness (does the
listing still exist/is it active, is the channel still configured at all)
and RATE/POLICY decisions (cooldown, the channel's explore/proven
eligibility mode) that change on their own schedule, independent of
whether the target itself is still real. Re-validating the WHOLE thing at
execute() time means a pending T2 proposal that simply cooled back into
ineligibility (a config edit renamed a channel, `config.apply()`'s
drift-repair rewrote `channels`, or a channel flipped explore->proven for
a listing that isn't proven) raises inside `execute()` -> a TERMINAL
`action.failed` (`runner._execute_and_record`) -- permanently burning the
action_id for a condition that was never permanent. `execute()` therefore
calls `_stale_check()` (genuine staleness only) and raises
`registry.StaleTargetError` for it; cooldown/eligibility-mode is instead
`governor.govern()`'s job (`RefusalReason.INELIGIBLE`, governor.py's own
comment), which REFUSES rather than terminalizes -- the action stays
"proposed"/approvable again once conditions change, exactly like
`listing.catalog_expand`'s own `RefusalReason.PACE` precedent (H1, same
review round).

## Channel identity (T5, /autoplan Eng Decision #9)

`target_id` is `"{listing_id}:{channel}"`, not a bare listing_id. Without
this, two drafts for the SAME listing on DIFFERENT channels (an instagram
caption and a facebook caption for the same print) would collide in the
runner's `(capability, target_id)` proposal dedup (`runner._pending_targets`/
`_supersede_siblings`) -- the second channel's draft would be silently
treated as a duplicate/competing proposal for the first's target, not a
genuinely independent one. Composing the channel into the target identity
(rather than minting `social.caption_draft.instagram`/`.facebook` as
separate capability keys) keeps ONE ladder/tier/cap-count for the
capability as a whole, which matches how the operator actually thinks
about it ("caption drafting", not two unrelated features) and needs no
change to the Capability protocol, the registry, or `governor.py`'s
per-capability caps -- both channels' executions still count toward the
SAME `daily_action_cap`/`per_capability_daily_cap`/`weekly_catalog_pct_cap`
entries, which is correct: they draw on the same limited "how much promo
noise is the operator willing to review" budget. `_parse_target_id()` below
is the inverse.

## Eligibility policy is a per-channel config field, not hardcoded here

`cfg.caption.channels[channel].eligibility` is `"explore"` or `"proven"`
(`_OpsSocialChannel`, models.py). This capability does NOT hardcode one
answer for every channel -- see `social.pinterest_post`'s own module
docstring and design doc `docs/designs/2026-08-24-pinterest-adapter-and-
loop-roadmap.md` §2.1 for why a PIN is explore-eligible (free, individually
deletable, one-of-dozens, and -- the load-bearing point -- a long-lived
SEARCH-INDEX entry that surfaces for months) while a promo caption on this
capability was originally, deliberately, proof-gated: it spends the shop's
audience attention on a ONE-SHOT FEED POST that dies in a day.

**Shipped default for both `instagram` and `facebook`: `"proven"`.** The
75%-of-historical-sales evidence is real, but it argues for *how often to
use this channel*, not for *loosening which listings get shown to it*. On
every axis §2.1 uses to justify Pinterest's explore policy, the operator's
own personal IG/FB feed lands on the SAME side as a cold feed, not the
Pinterest side:
  - **Free?** Yes for both -- not the differentiator.
  - **Individually deletable?** Yes for both -- also not the differentiator.
  - **One-of-dozens, forgiving of a miss?** A Pinterest board holds dozens
    of pins; a personal feed post is seen once, in a stream, and competes
    with the rest of that person's social graph for a few seconds of
    attention -- arguably LESS forgiving than a pin, not more, because it's
    the operator's own reputation with real people, not an anonymous search
    index.
  - **Long-lived search-index entry vs. one-shot feed post that dies in a
    day?** This is the one §2.1 calls "the load-bearing difference," and
    IG/FB feed posts fail it outright -- they are not search-indexed, they
    are not discoverable months later, they die in the feed algorithm
    within about a day, exactly the profile §2.1 explicitly carves OUT of
    the explore policy (`docs/research/2026-08-24-etsy-path-to-
    profitability.md` line ~65 makes the identical point about *why*
    Pinterest is unusually good for this: "unlike a social feed post that
    dies in a day").

So the honest conclusion, applying the same framework the operator's own
prior review used, is: the owned network is MORE valuable (it converts at
a rate the shop's cold channels have never matched) but not MORE
FORGIVING of showing an unproven listing to it -- if anything a bad post to
one's own friends/family costs real social capital a bad pin never does.
`"explore"` remains available as a real, tested config value (a future
channel with a genuinely different risk profile, or an operator override
once there's evidence the bar should move) -- it is simply not the default
either channel ships with. Widening it later is a one-line config change,
never a code change.

## `mark_posted` / cooldown (T5+E5)

`mark_posted()` (imported from `pipeline.ops.social`, the mechanism shared
with `social.pinterest_post`) appends `social.caption_posted`, joined to
its `social.caption_drafted` event by a directly-stored `action_id` (no
utm-embedding needed -- a caption has no destination URL to carry a join
key in). This closes the gap the T5 brief named: without it, a
drafted-but-never-posted caption would block its own (listing, channel)
for the whole cooldown with no way to tell drafted from actually-posted.
`_candidates()`'s cooldown check reads BOTH `social.caption_drafted` and
`social.caption_posted` for the (listing_id, channel) pair -- so a
proposal the operator never even approved still holds the cooldown (it's
still "recently offered," which is the actual anti-spam property both
policies want), while `mark_posted()` gives the operator a way to signal
"I really did post this" for anything that needs to be distinguishable
later (a future outcome reader, mirroring `pin_experiment_readout`)."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops import social as _social
from shopsteward.pipeline.ops.config import get_ops_config, ops_config_hash
from shopsteward.pipeline.ops.models import (
    ExecutionResult,
    ListingSales,
    OpsConfig,
    ProposedAction,
    Tier,
)
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id
from shopsteward.pipeline.ops.timeutil import parse_ts

_CAPTION_EVENT_TYPES = ("social.caption_drafted", "social.caption_posted")


def _target_id(listing_id: int, channel: str) -> str:
    return f"{listing_id}:{channel}"


def _parse_target_id(target_id: str) -> tuple[int, str] | None:
    """Inverse of `_target_id()` -- returns None for a malformed/legacy
    (pre-T5, bare-listing-id) target_id rather than raising, so a stale or
    hand-forged action_id is treated as ungrounded, never a crash."""
    listing_str, sep, channel = target_id.partition(":")
    if not sep or not channel or not listing_str.isdigit():
        return None
    return int(listing_str), channel


def _is_active(conn: sqlite3.Connection, user_id: int, listing_id: int) -> bool:
    """Same state check as `listing.seo_edit`'s `_eligible` -- proj_listings
    already reflects the latest sync. A listing deactivated since it last
    sold must never get a promo caption; that just drives traffic to a
    delisted item."""
    row = conn.execute(
        "SELECT state FROM proj_listings WHERE user_id=? AND listing_id=?",
        (user_id, listing_id),
    ).fetchone()
    return row is not None and row["state"] == "active"


def _last_drafted_or_posted_at(
    conn: sqlite3.Connection, user_id: int, listing_id: int, channel: str
) -> str | None:
    """Most recent `social.caption_drafted`/`social.caption_posted`
    created_at for this (listing_id, channel) pair, or None if never drafted
    -- the cooldown grounding for BOTH eligibility policies (module
    docstring). `parse_ts()`-based real datetime compare, same E2 precedent
    `pinterest_post._last_pinned_at` uses -- never a raw string compare
    between the DB's 'Z'-suffix `created_at` and a freshly computed
    `.isoformat()` cutoff."""
    latest: str | None = None
    for event_type in _CAPTION_EVENT_TYPES:
        for e in read_all(conn, event_type):
            if e.user_id != user_id:
                continue
            if e.payload.get("listing_id") != listing_id or e.payload.get("channel") != channel:
                continue
            if e.created_at and (latest is None or parse_ts(e.created_at) > parse_ts(latest)):
                latest = e.created_at
    return latest


def _within_cooldown(
    conn: sqlite3.Connection, user_id: int, listing_id: int, channel: str, cutoff: datetime
) -> bool:
    last = _last_drafted_or_posted_at(conn, user_id, listing_id, channel)
    return last is not None and parse_ts(last) >= cutoff


def _explore_candidates(
    conn: sqlite3.Connection, user_id: int, channel: str, cooldown_days: int, now: datetime
) -> dict[str, ListingSales]:
    """Coverage-first (mirrors `pinterest_post._candidates()`'s own policy,
    minus the image requirement -- a caption needs no image): every ACTIVE
    listing not drafted/posted on THIS channel within `cooldown_days`.
    `units`/`revenue_usd` are 0 by construction (this arm makes no sales
    claim at all) -- `proof_phrase()` already handles a zero-units row
    honestly."""
    cutoff = now - timedelta(days=cooldown_days)
    out: dict[str, ListingSales] = {}
    rows = conn.execute(
        "SELECT listing_id, title FROM proj_listings WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall()
    for r in rows:
        if _within_cooldown(conn, user_id, r["listing_id"], channel, cutoff):
            continue
        out[_target_id(r["listing_id"], channel)] = ListingSales(
            listing_id=r["listing_id"], title=r["title"], units=0, revenue_usd=0.0
        )
    return out


def _proven_candidates(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    channel: str,
    cooldown_days: int,
    now: datetime,
) -> dict[str, ListingSales]:
    """Proof-first (the ORIGINAL, still-default policy): every listing
    `analytics.proven_listings()` blesses (T12: a sale within the trailing
    90 days, a lifetime sale, or a views-velocity signal), still
    `state=="active"`, AND outside THIS channel's own cooldown -- without
    the cooldown check here too, an approved proven caption would be a
    valid re-candidate again on the very next `ops run` for as long as the
    listing stays proven, defeating the point of a cooldown entirely."""
    cutoff = now - timedelta(days=cooldown_days)
    out: dict[str, ListingSales] = {}
    for s in analytics.proven_listings(conn, user_id, cfg):
        if not _is_active(conn, user_id, s.listing_id):
            continue
        if _within_cooldown(conn, user_id, s.listing_id, channel, cutoff):
            continue
        out[_target_id(s.listing_id, channel)] = s
    return out


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, *, now: datetime | None = None
) -> dict[str, ListingSales]:
    """target_id (`"{listing_id}:{channel}"`) -> the grounding facts for it
    -- the ONE grounding function shared by materialize()/execute() (M8b
    slice-2 planner-safety contract), folding EVERY configured channel's own
    eligibility policy (module docstring). `now` is a comparison-friendly
    injection point (E2 precedent) -- tests pin a fixed instant to exercise
    the cooldown boundary exactly; every real caller omits it and gets
    wall-clock UTC."""
    now = now or datetime.now(UTC)
    out: dict[str, ListingSales] = {}
    for channel, channel_cfg in cfg.caption.channels.items():
        if channel_cfg.eligibility == "explore":
            out.update(_explore_candidates(conn, user_id, channel, channel_cfg.cooldown_days, now))
        else:
            out.update(
                _proven_candidates(conn, user_id, cfg, channel, channel_cfg.cooldown_days, now)
            )
    return out


def _stale_check(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, listing_id: int, channel: str
) -> ListingSales | None:
    """H2a (guardrail review, 2026-08-25): `execute()`'s own re-validation
    predicate -- GENUINE per-target staleness only. Deliberately narrower
    than `_candidates()`: does the listing still exist and is it still
    active, and is `channel` still a configured caption channel at all.
    Deliberately excludes cooldown and eligibility-mode (explore/proven) --
    those are RATE/POLICY decisions that change independently of whether
    THIS target is actually still real, and belong to `governor.govern()`'s
    `RefusalReason.INELIGIBLE` check (governor.py's own comment) so an
    over-cooldown or newly-ineligible approval is REFUSED, never terminally
    failed. `_candidates()` (the full grounding+policy set) stays the
    propose()/materialize()/governor predicate -- only `execute()` uses
    this narrower one."""
    if channel not in cfg.caption.channels:
        return None  # the channel itself was removed/renamed since propose()
    if not _is_active(conn, user_id, listing_id):
        return None
    row = conn.execute(
        "SELECT title FROM proj_listings WHERE user_id=? AND listing_id=?",
        (user_id, listing_id),
    ).fetchone()
    if row is None:  # pragma: no cover - _is_active() already implies a row exists
        return None
    return ListingSales(listing_id=listing_id, title=row["title"], units=0, revenue_usd=0.0)


def _valid_caption(caption: object, cfg: OpsConfig) -> str | None:
    """Structural validation only (drop, never truncate/clamp): a non-empty
    (after stripping whitespace) `str` within Instagram's own real caption
    limit -- the limit is checked against the ORIGINAL string (Instagram
    counts the raw length), the caption itself is never stripped/altered."""
    if not isinstance(caption, str) or not caption.strip():
        return None
    if len(caption) > cfg.caption.max_len:
        return None
    return caption


def _build_action(
    target: ListingSales, channel: str, caption: str, cfg_hash: str, today: str, expires_at: str
) -> ProposedAction:
    target_id = _target_id(target.listing_id, channel)
    raw = "|".join((target_id, str(target.units), hashlib.sha256(caption.encode()).hexdigest()))
    inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
    action_id = compute_action_id("social.caption_draft", target_id, inputs_hash, cfg_hash, today)
    return ProposedAction(
        action_id=action_id,
        capability="social.caption_draft",
        target_type="listing",
        target_id=target_id,
        tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
        reason=f"{analytics.proof_phrase(target)} -- promo caption ready to post ({channel}).",
        inputs_hash=inputs_hash,
        estimated_cost_usd=0.0,
        undo_available=False,
        expires_at=expires_at,
        params={"caption": caption, "listing_id": target.listing_id, "channel": channel},
    )


def _resolve_caption_drafted(conn: sqlite3.Connection, user_id: int, action_id: str) -> Event:
    """`social.social.mark_posted()`'s pluggable resolver for this channel
    family -- a caption's drafted event stores its own full `action_id`
    directly (no utm-style indirection needed), so this is a plain exact
    match. Raises ValueError (never returns None) so `mark_posted()` can
    stay generic across channels -- see that module's own docstring."""
    drafted = next(
        (
            e
            for e in read_all(conn, "social.caption_drafted")
            if e.user_id == user_id and e.payload.get("action_id") == action_id
        ),
        None,
    )
    if drafted is None:
        raise ValueError(f"no drafted caption found for action_id {action_id!r}")
    return drafted


def mark_posted(conn: sqlite3.Connection, user_id: int, action_id: str) -> bool:
    """`ops mark-posted` (cli.py, tried after `pinterest_post.mark_posted`
    fails): append `social.caption_posted` for the `social.caption_drafted`
    event whose OWN `action_id` matches. Returns True if a new event was
    appended, False if this action_id was already marked posted (safe
    no-op). Raises ValueError if no drafted caption matches `action_id` at
    all -- never a partial/crashing write."""
    return _social.mark_posted(
        conn,
        user_id,
        action_id,
        posted_event_type="social.caption_posted",
        resolve_drafted=_resolve_caption_drafted,
    )


class SocialCaptionDraft:
    key = "social.caption_draft"
    # T2 ceiling -- NEVER promotable (marketing copy addressed to the shop's
    # audience is worth an operator glance regardless of the ladder).
    # registry.py's invariant 2 enforces there is no config path that can
    # raise this Python ceiling.
    max_tier = Tier.PROPOSE
    policy_verified = True  # same approved LLM use as listing.seo_edit copy; nothing is published.
    # No undo path: allowed because max_tier is not below PROPOSE
    # (registry.py invariant 1) -- the reversal is "don't post it", never a
    # programmatic undo (listing.gapfill_reprint's own precedent).
    undo = None

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return []  # planner-only -- see module docstring.

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        target = _candidates(conn, user_id, cfg).get(intent.target_id)
        if target is None:
            return None  # ungrounded (hallucinated, non-selling, cooling down, or malformed)
        parsed = _parse_target_id(intent.target_id)
        if parsed is None:
            return None  # defensive: _candidates() keys are well-formed; never trust the LLM's echo
        _listing_id, channel = parsed

        caption = _valid_caption(intent.params.get("caption"), cfg)
        if caption is None:
            return None  # empty, non-str, or over the platform limit -- dropped, never clamped

        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
        return _build_action(target, channel, caption, cfg_hash, today, expires_at)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        parsed = _parse_target_id(action.target_id)
        if parsed is None:
            # M3 (guardrail review, 2026-08-25): a legacy/malformed
            # target_id (e.g. a pre-T5 bare listing_id, no ":channel")
            # is deliberately plain ValueError, NOT StaleTargetError -- the
            # runner's safe default (action.refused, never a terminal
            # action.failed) lets a legacy pending proposal in the live
            # shop's NEEDS-YOU queue be cleanly refused/expired rather than
            # permanently burned on approve.
            raise ValueError(
                f"action {action.action_id}: malformed target_id {action.target_id!r} -- "
                "refusing caption draft"
            )
        listing_id, channel = parsed
        cfg = get_ops_config(conn, user_id)
        # H2a: `_stale_check()`, NOT `_candidates()` -- genuine per-target
        # staleness only (listing gone/inactive, channel removed). Cooldown/
        # eligibility-mode is `governor.govern()`'s job (RefusalReason.
        # INELIGIBLE) and must already have refused this action BEFORE
        # execute() is ever reached; if it somehow wasn't (a race between
        # approval and execute, or a direct cap.execute() call bypassing the
        # runner entirely), that is still a real inconsistency worth
        # refusing -- just never worth permanently burning the action_id
        # for, since the underlying condition is temporary.
        target = _stale_check(conn, user_id, cfg, listing_id, channel)
        if target is None:
            raise StaleTargetError(
                f"listing {listing_id} ({channel}): no longer exists/active, or the channel "
                "was removed -- refusing caption draft"
            )

        caption = _valid_caption(action.params.get("caption"), cfg)
        if caption is None:
            # Deliberately plain ValueError, not StaleTargetError -- a bad
            # PARAMS problem (max_len shrunk in config, or a hand-forged
            # action), not the target itself being stale.
            raise ValueError(
                f"action {action.action_id}: params.caption is no longer valid -- "
                "refusing caption draft"
            )

        # The ONLY effect of this capability: an assist artifact for the
        # operator to copy-paste. No Meta/IG/FB call, no publish, ever.
        append(
            conn,
            Event(
                user_id=user_id,
                type="social.caption_drafted",
                payload={
                    "listing_id": listing_id,
                    "channel": channel,
                    "caption": caption,
                    "title": target.title,
                    "action_id": action.action_id,
                    "drafted_at": datetime.now(UTC).isoformat(),
                },
            ),
        )
        return ExecutionResult(
            before={},
            after={"listing_id": listing_id, "channel": channel, "chars": len(caption)},
            cost_usd=0.0,
            duration_ms=0,
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
