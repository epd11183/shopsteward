"""`listing.size_claims_fix` -- proposes a deterministic DESCRIPTION-only edit for
live digital listings that already received the multi-ratio backfill crops but
whose description still names print sizes the file plan cannot deliver (design
2026-11-21-multi-ratio-digital-files section 8, slice 5a). The operator approves
each edit -- until 2026-11-22, when the operator decided (explicitly) that listing
edits of every kind need no approval: `max_tier = Tier.AUTO`, started at AUTO by
config `autonomy.tier_floor`. Controls now: event log, undo (restores the prior
description), live gates, spend cap. A description edit can still move Etsy search
ranking (not restorable via API) -- accepted by the operator.

A sibling of `listing.seo_edit`, not a branch of it: seo_edit.propose() is
planner-only by contract and returns []. The runner keys pending/duplicate/
supersede on (capability, target_id), and the governor's cooldown and pin holdout
name seo_edit/renew keys, so a queued or declined seo_edit proposal for the same
listing neither blocks nor collides with this one.

Eligibility (one grounding function, `_candidates`, for propose/materialize):
- a `listingdraft.backfill_planned` event exists and the listing is active;
- Etsy's file list shows every planned non-full crop (matched to a non-removed
  `file_backfilled` event by key and file id);
- the latest `etsy.listing.observed` snapshot carries a description baseline that
  currently claims an undelivered size;
- `fix_description` returns a rewrite (else: hand edit).

The `full` entry's sizes count as delivered only when the kept legacy rank-1 file
has exactly the source's byte size (identical file); otherwise they are cleared
from the plan used for both the check and the rewrite. Etsy's file list is the only
source of that size (backfill events never record the legacy file), so propose()
needs a working write adapter; a listing whose file list cannot be read is skipped,
never proposed with a guess.

execute() writes ONLY `description` and re-validates against the latest observed
snapshot (a changed description raises StaleTargetError). Known limitation: the
write adapter has no listing read and the snapshot is refreshed by sync, so until
the next sync the baseline is the pre-edit text; an executed-not-undone fix with
the same baseline is therefore not re-proposed, but an unrelated description edit
by seo_edit in that same window would be overwritten by this capability's undo.
"""

import hashlib
import html
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.etsy.models import EtsyListingUpdate, ListingFile
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.backfill import (
    BACKFILLED,
    PLANNED,
    REMOVED,
    _events,
    _recorded_plan,
    _snapshots,
)
from shopsteward.pipeline.listings.digital_files import DigitalFilesConfig, FilePlan
from shopsteward.pipeline.listings.size_claims import fix_description
from shopsteward.pipeline.listings.size_guard import undelivered_sizes
from shopsteward.pipeline.listings.source_assets import resolve_source
from shopsteward.pipeline.ops.capabilities.seo_edit import _MAX_DESCRIPTION_LEN
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.projections import action_rows
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id

KEY = "listing.size_claims_fix"


def _refresh(conn: sqlite3.Connection) -> None:
    """`ops run` only rebuilds core/ops; source resolution and the listing config
    need these (catalog_expand's rebuild_listings precedent)."""
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.projections import rebuild_pipeline

    rebuild_pipeline(conn)
    rebuild_listings(conn)


def _complete(events: list, files: list[ListingFile], plan: FilePlan) -> bool:
    removed = {e.payload["etsy_file_id"] for e in events if e.type == REMOVED}
    on_etsy = {f.listing_file_id for f in files}
    done = {
        e.payload["key"]
        for e in events
        if e.type == BACKFILLED
        and e.payload["etsy_file_id"] not in removed
        and e.payload["etsy_file_id"] in on_etsy
    }
    return all(e.key in done for e in plan.files if e.key != "full")


def _rank1_is_source(
    conn: sqlite3.Connection, user_id: int, lid: int, events: list, files: list[ListingFile]
) -> bool:
    """The kept legacy rank-1 file is byte-identical to the source (same size)."""
    mine = {e.payload["etsy_file_id"] for e in events if e.type == BACKFILLED}
    legacy = sorted(
        (f for f in files if f.listing_file_id not in mine), key=lambda f: f.rank or 10**6
    )
    ref = resolve_source(conn, user_id, lid)
    if not legacy or legacy[0].size_bytes is None or ref is None or not ref.on_disk_present:
        return False
    try:
        return os.path.getsize(ref.on_disk_path or "") == legacy[0].size_bytes
    except OSError:
        return False


def _delivered_plan(
    conn: sqlite3.Connection, user_id: int, lid: int, adapter: EtsyWriteAdapter
) -> FilePlan | None:
    """The recorded backfill plan, clipped to what Etsy really delivers; None when
    there is no plan, no readable file list, or the backfill is incomplete."""
    events = _events(conn, user_id, lid)
    last = next((e.payload for e in reversed(events) if e.type == PLANNED), None)
    plan = _recorded_plan(last)
    if plan is None:
        return None
    try:
        files = adapter.list_listing_files(lid)
    except EtsyWriteError:
        return None  # no file list -> no rank-1 evidence -> never guess
    if not _complete(events, files, plan):
        return None
    if not _rank1_is_source(conn, user_id, lid, events, files):
        plan = plan.model_copy(
            update={
                "files": [
                    e.model_copy(update={"sizes": []}) if e.key == "full" else e for e in plan.files
                ]
            }
        )
    return plan


def _already_fixed(conn: sqlite3.Connection, user_id: int, lid: int, old: str) -> bool:
    """An executed (not undone) fix for this exact baseline: the snapshot is simply
    not refreshed yet."""
    for r in action_rows(conn):
        if (
            r["user_id"] == user_id
            and r["capability"] == KEY
            and r["target_id"] == str(lid)
            and r["state"] == "executed"
            and json.loads(r["before_json"] or "{}").get("description") == old
        ):
            return True
    return False


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, adapter: EtsyWriteAdapter
) -> dict[str, ProposedAction]:
    _refresh(conn)
    try:
        dcfg: DigitalFilesConfig | None = listing_config.get_config(conn, user_id).digital_files
    except KeyError:
        return {}
    if dcfg is None:
        return {}

    snaps = _snapshots(conn, user_id)
    lids = sorted(
        {int(e.payload["etsy_listing_id"]) for e in read_all(conn, PLANNED) if e.user_id == user_id}
    )
    today_date = datetime.now(UTC).date()
    cfg_hash = ops_config_hash(cfg)
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
    out: dict[str, ProposedAction] = {}
    for lid in lids:
        snap = snaps.get(lid)
        if snap is None or snap.state != "active" or not snap.description:
            continue
        plan = _delivered_plan(conn, user_id, lid, adapter)
        if plan is None:
            continue
        # Etsy returns descriptions HTML-escaped ("&#39;"); work on, preview and send
        # plain text so buyers never see a literal entity.
        old = html.unescape(snap.description)
        claimed = undelivered_sizes([old], plan, dcfg)
        new = fix_description(old, plan, dcfg) if claimed else None
        if new is None or _already_fixed(conn, user_id, lid, old):
            continue
        inputs_hash = hashlib.sha256(f"{lid}|{old}|{new}".encode()).hexdigest()
        out[str(lid)] = ProposedAction(
            action_id=compute_action_id(
                KEY, str(lid), inputs_hash, cfg_hash, today_date.isoformat()
            ),
            capability=KEY,
            target_type="listing",
            target_id=str(lid),
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=(
                f"{snap.title} -- description claims sizes the files do not deliver "
                f"({', '.join(claimed)}); remove them (description only, undo restores)."
            ),
            inputs_hash=inputs_hash,
            estimated_cost_usd=0.0,
            undo_available=True,
            expires_at=expires_at,
            params={"listing_id": lid, "old_description": old, "new_description": new},
        )
    return out


class ListingSizeClaimsFix:
    key = KEY
    # 2026-11-22 operator decision: no approval (module docstring).
    max_tier = Tier.AUTO
    policy_verified = True  # same updateListing/listings_w description edit as seo_edit (E3).

    def __init__(self, adapter: EtsyWriteAdapter) -> None:
        self._adapter = adapter

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return list(_candidates(conn, user_id, cfg, self._adapter).values())

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        return _candidates(conn, user_id, cfg, self._adapter).get(intent.target_id)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        old = action.params.get("old_description")
        new = action.params.get("new_description")
        if (
            not isinstance(old, str)
            or not isinstance(new, str)
            or new == old
            or not (1 <= len(new) <= _MAX_DESCRIPTION_LEN)
        ):
            raise ValueError(f"action {action.action_id}: invalid params -- refusing edit")
        snap = _snapshots(conn, user_id).get(listing_id)
        if snap is None or html.unescape(snap.description or "") != old:
            raise StaleTargetError(
                f"listing {listing_id}: description changed since this was proposed -- refusing"
            )
        self._adapter.update_listing(listing_id, EtsyListingUpdate(description=new))
        return ExecutionResult(
            before={"description": old}, after={"description": new}, cost_usd=0.0, duration_ms=0
        )

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        prior: dict | None = None
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                prior = e.payload["before"]
        if prior is None:
            return
        # the snapshot refreshes only at the next sync: it is still `old` (not yet
        # synced) or `new`; anything else was edited since -- never overwrite it.
        snap = _snapshots(conn, user_id).get(int(action.target_id))
        if snap is None or html.unescape(snap.description or "") not in (
            prior["description"],
            action.params.get("new_description"),
        ):
            raise StaleTargetError(
                f"listing {action.target_id}: description edited since this fix -- refusing undo"
            )
        self._adapter.update_listing(
            int(action.target_id), EtsyListingUpdate(description=prior["description"])
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
