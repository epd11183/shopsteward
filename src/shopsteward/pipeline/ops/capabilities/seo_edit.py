"""`listing.seo_edit` -- Claude rewrites a listing's title/tags, the operator
approves each edit (M8b slice 4a, design §4/§8 slice 4). Policy: Etsy E3
PERMITTED via `updateListing`/`listings_w`. Draft #10: title/tag edit is a
**T2 ceiling, NEVER higher** (the formula would say T1, but overridden --
§0 P3 policy surface + a bad edit resets a listing's Etsy search-ranking
history, which is not cleanly reversible even though the fields themselves
are; PRD §5.5 "wait at Gate 3"). `max_tier = Tier.PROPOSE` and this
capability is never promoted regardless of the ladder (registry.py's
invariant 2 -- max_tier is a Python attribute, not config).

`description` editing is DEFERRED, this slice only: `EtsyListing`
(adapters/etsy/models.py) -- the model every `etsy.listing.observed` event
carries -- has no `description` field at all, so there is no baseline to
diff a new description against or restore on undo. Adding description
requires first adding it to the sync/read model (a later slice); until
then a `description` key in `intent.params`/`action.params` is simply
never read, validated, sent, or restored -- only `title`/`tags` are.

**Planner-only, like `ops.tune_threshold`'s trigger is SQL but this
capability's COPY is not**: `propose()` always returns `[]` -- writing good
SEO copy is exactly the "deterministic heuristic too blunt" case (design
§11.1), there is no sensible deterministic title/tags to generate. All the
value is in `materialize()`: the LLM writes the copy, `_validate_params()`
validates it against Etsy's real field limits (dropped, never clamped) and
against the listing's current title/tags (a no-op proposal is never built).

**Both digital AND POD listings are eligible** -- unlike `listing.reprice`
(digital-only, draft #9b), SEO edit only ever calls `update_listing` with
title/tags, which does not touch SKUs, variation structure, price, state,
or the production-partner declaration, so the POD-first rule ("never modify
provider-set SKU values or variation structure") is preserved regardless of
product type. `_eligible()` is the ONE grounding function shared by
propose() (which yields nothing) and materialize()/execute() -- the same
M8b slice-2 planner-safety contract every capability here follows.

Holds its own EtsyWriteAdapter, injected at construction (the chassis
contract -- autorenew.py precedent). This module never imports or
constructs an adapter itself."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pydantic

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListing, EtsyListingUpdate
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import read_all
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.ops.config import get_ops_config, ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id

# Etsy's real field limits (not tuning knobs -- adapters/copy/interface.py's
# CopyVerdict enforces the same numbers on the deterministic listing-copy
# path; kept as local constants here rather than imported since those are
# private to that module).
_MAX_TITLE_LEN = 140
_MAX_TAGS = 13
_MAX_TAG_LEN = 20


@dataclass(frozen=True)
class _Target:
    listing_id: int
    current_title: str
    current_tags: list[str]
    lifetime_views: int


def _latest_observed(conn: sqlite3.Connection, user_id: int, listing_id: int) -> EtsyListing | None:
    """The most recent VALID etsy.listing.observed snapshot for this listing
    (autorenew.py precedent) -- the only place `tags` (not carried by
    proj_listings) is readable from."""
    latest: EtsyListing | None = None
    for e in read_live_observed(conn, "etsy.listing.observed"):
        if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
            continue
        try:
            latest = EtsyListing.model_validate(e.payload)
        except pydantic.ValidationError:
            continue
    return latest


def _eligible(conn: sqlite3.Connection, user_id: int, cfg: OpsConfig) -> dict[str, _Target]:
    """target_id -> the eligible-for-SEO-edit facts for that listing -- the
    ONE grounding function shared by materialize() and execute() so the two
    can never disagree. "Viewed but not selling -> copy/SEO may be the
    problem" (the same signal analytics.viewed_not_sold surfaces on the
    Brief, applied here with a revenue-window bound rather than lifetime, to
    match reprice's own eligibility shape). No digital/POD split -- see
    module docstring."""
    start = datetime.now(UTC).date() - timedelta(days=cfg.windows.revenue_window_days - 1)
    end = datetime.now(UTC).date()

    rows = conn.execute(
        "SELECT listing_id, title, views FROM proj_listings WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall()

    out: dict[str, _Target] = {}
    for r in rows:
        if r["views"] < cfg.seo_edit.min_lifetime_views:
            continue
        sold_in_window = conn.execute(
            "SELECT 1 FROM proj_sale_items WHERE user_id=? AND listing_id=? "
            "AND sale_date BETWEEN ? AND ? LIMIT 1",
            (user_id, r["listing_id"], start.isoformat(), end.isoformat()),
        ).fetchone()
        if sold_in_window is not None:
            continue
        listing = _latest_observed(conn, user_id, r["listing_id"])
        if listing is None:
            continue  # defensive -- proj_listings is built from the same events
        out[str(r["listing_id"])] = _Target(
            listing_id=r["listing_id"],
            current_title=r["title"],
            current_tags=listing.tags,
            lifetime_views=r["views"],
        )
    return out


def _validate_params(params: dict, target: _Target) -> dict[str, str | list[str]] | None:
    """Structural validation against Etsy's real limits (drop, never clamp)
    plus a diff against `target`'s current title/tags -- returns only the
    fields that are both valid AND actually changed, or None if nothing
    survives. A `description` key in `params` is silently ignored (never
    read, never validated, never sent -- see module docstring). Shared by
    materialize() and execute() (re-validation) so the two rules can never
    drift apart."""
    title = params.get("title")
    new_title: str | None = None
    if title is not None:
        if not isinstance(title, str) or not (1 <= len(title) <= _MAX_TITLE_LEN):
            return None
        new_title = title

    tags = params.get("tags")
    new_tags: list[str] | None = None
    if tags is not None:
        if not isinstance(tags, list) or not (1 <= len(tags) <= _MAX_TAGS):
            return None
        if not all(isinstance(t, str) and 1 <= len(t) <= _MAX_TAG_LEN for t in tags):
            return None
        new_tags = tags

    changed: dict[str, str | list[str]] = {}
    if new_title is not None and new_title != target.current_title:
        changed["title"] = new_title
    if new_tags is not None and new_tags != target.current_tags:
        changed["tags"] = new_tags
    return changed or None


def _build_action(
    target: _Target,
    changed: dict[str, str | list[str]],
    cfg: OpsConfig,
    cfg_hash: str,
    today: str,
    expires_at: str,
) -> ProposedAction:
    raw = "|".join((str(target.listing_id), json.dumps(changed, sort_keys=True)))
    inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
    action_id = compute_action_id(
        "listing.seo_edit", str(target.listing_id), inputs_hash, cfg_hash, today
    )
    return ProposedAction(
        action_id=action_id,
        capability="listing.seo_edit",
        target_type="listing",
        target_id=str(target.listing_id),
        tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
        reason=(
            f"{target.lifetime_views} views, 0 sales in the last "
            f"{cfg.windows.revenue_window_days}d -- refresh title/tags."
        ),
        inputs_hash=inputs_hash,
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=expires_at,
        params=changed,
    )


class ListingSeoEdit:
    key = "listing.seo_edit"
    # T2 ceiling -- NEVER promotable (draft #10: a bad SEO edit resets Etsy's
    # search-ranking history, which isn't cleanly reversible even though the
    # fields are). registry.py's invariant 2 enforces there is no config path
    # that can raise this.
    max_tier = Tier.PROPOSE
    policy_verified = True  # Etsy E3 permitted.

    def __init__(self, adapter: EtsyWriteAdapter) -> None:
        self._adapter = adapter

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return []  # planner-only -- see module docstring.

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        target = _eligible(conn, user_id, cfg).get(intent.target_id)
        if target is None:
            return None  # ungrounded (hallucinated or ineligible target)

        changed = _validate_params(intent.params, target)
        if changed is None:
            return None  # invalid, or no actual change -- dropped, never clamped

        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
        return _build_action(target, changed, cfg, cfg_hash, today, expires_at)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        cfg = get_ops_config(conn, user_id)
        target = _eligible(conn, user_id, cfg).get(action.target_id)
        if target is None:
            raise ValueError(f"listing {listing_id}: no longer active/eligible -- refusing edit")

        changed = _validate_params(action.params, target)
        if changed is None:
            raise ValueError(
                f"action {action.action_id}: params {action.params!r} are no longer valid or "
                "a no-op -- refusing edit"
            )

        before: dict[str, str | list[str]] = {}
        if "title" in changed:
            before["title"] = target.current_title
        if "tags" in changed:
            before["tags"] = target.current_tags

        self._adapter.update_listing(
            listing_id,
            EtsyListingUpdate(title=changed.get("title"), tags=changed.get("tags")),
        )
        return ExecutionResult(before=before, after=dict(changed), cost_usd=0.0, duration_ms=0)

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        prior: dict | None = None
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                prior = e.payload["before"]
        if prior is None:
            return  # nothing was actually executed -- runner already guards this, but be safe

        listing_id = int(action.target_id)
        self._adapter.update_listing(
            listing_id,
            EtsyListingUpdate(title=prior.get("title"), tags=prior.get("tags")),
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
