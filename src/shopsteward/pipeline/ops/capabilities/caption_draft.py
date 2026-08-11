"""`social.caption_draft` -- Claude writes a promo social caption for a
PROVEN best-seller; the operator copy-pastes it to IG/FB and posts MANUALLY
(M8b slice 6, design §10 CMO / draft §3.3 #26). **No Meta/IG/FB call, ever**
-- IG/FB auto-publish is blocked on Meta App Review + Business Verification
(`docs/policy/2026-08-11-autonomy-platform-policy.md`); `adapters/meta`
stays unwired. This capability's entire output is one `social.caption_drafted`
event; the reversal is trivial -- the operator simply never posts it.

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
generate. `_candidates()` is the ONE grounding function shared by
materialize()/execute() (M8b slice-2 planner-safety contract): only a
listing `analytics.top_sellers()` already blessed (real sales in the
window) AND still `state=="active"` (same check `listing.seo_edit`'s
`_eligible` uses) is ever eligible, so a hallucinated, non-selling, or
since-deactivated target is always dropped, never guessed. The LLM's
caption text is validated structurally (non-empty after stripping, `str`,
`<= cfg.caption.max_len` -- Instagram's real limit) and DROPPED, never
truncated -- the SQL-derived `reason` on the action is never the caption
text itself."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import Event, append
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.config import get_ops_config, ops_config_hash
from shopsteward.pipeline.ops.models import (
    ExecutionResult,
    ListingSales,
    OpsConfig,
    ProposedAction,
    Tier,
)
from shopsteward.pipeline.ops.registry import compute_action_id


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


def _candidates(conn: sqlite3.Connection, user_id: int, cfg: OpsConfig) -> dict[str, ListingSales]:
    """target_id (`str(listing_id)`) -> the proven-top-seller facts for it --
    the ONE grounding function shared by materialize()/execute() so a
    hallucinated, non-selling, or now-inactive target is always dropped,
    never guessed."""
    return {
        str(s.listing_id): s
        for s in analytics.top_sellers(conn, user_id, cfg)
        if _is_active(conn, user_id, s.listing_id)
    }


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
    target: ListingSales, caption: str, cfg_hash: str, today: str, expires_at: str
) -> ProposedAction:
    raw = "|".join(
        (str(target.listing_id), str(target.units), hashlib.sha256(caption.encode()).hexdigest())
    )
    inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
    action_id = compute_action_id(
        "social.caption_draft", str(target.listing_id), inputs_hash, cfg_hash, today
    )
    return ProposedAction(
        action_id=action_id,
        capability="social.caption_draft",
        target_type="listing",
        target_id=str(target.listing_id),
        tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
        reason=f"top seller ({target.units} sold) -- promo caption ready to post.",
        inputs_hash=inputs_hash,
        estimated_cost_usd=0.0,
        undo_available=False,
        expires_at=expires_at,
        params={"caption": caption, "listing_id": target.listing_id},
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
            return None  # ungrounded (hallucinated or non-selling target)

        caption = _valid_caption(intent.params.get("caption"), cfg)
        if caption is None:
            return None  # empty, non-str, or over the platform limit -- dropped, never clamped

        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
        return _build_action(target, caption, cfg_hash, today, expires_at)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        cfg = get_ops_config(conn, user_id)
        target = _candidates(conn, user_id, cfg).get(action.target_id)
        if target is None:
            raise ValueError(
                f"listing {listing_id}: no longer a top seller -- refusing caption draft"
            )

        caption = _valid_caption(action.params.get("caption"), cfg)
        if caption is None:
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
                    "caption": caption,
                    "title": target.title,
                    "drafted_at": datetime.now(UTC).isoformat(),
                },
            ),
        )
        return ExecutionResult(
            before={},
            after={"listing_id": listing_id, "chars": len(caption)},
            cost_usd=0.0,
            duration_ms=0,
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
