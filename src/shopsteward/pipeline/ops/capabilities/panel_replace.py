"""`listing.panel_replace` -- swaps the stale "what you get" gallery image on a live
digital listing for one rendered from the sizes its files now deliver (design
2026-11-21-multi-ratio-digital-files section 8 B3, slice 5b). Tier AUTO from
config `autonomy.tier_floor` (operator, 2026-11-22: listing edits need no
approval); undo is real, so it is allowed.

How the panel is identified (never guessed): the pipeline's push recorded, in
`proj_listing_drafts.images_json`, exactly one `digital_whatyougot` entry with
an `etsy_image_id` and a rank, counted across ALL of the listing's pushed/published
drafts. Zero or several such entries (adopted drafts have none) mean no proposal.
The sizes that panel
was rendered with are the entry's `panel_sizes` (set by a previous swap) or else
the `mockup.generated` params of the entry's path; no recorded render, no proposal.

The delivered plan is the backfill's (`size_claims_fix._delivered_plan`), falling
back to the draft's `files_plan_json` when no backfill ever ran. A panel is stale
when its recorded sizes differ from that plan's sizes.

execute: render with `render_digital_whatyougot` (the mockup job's renderer),
upload at the old rank, delete the old image (Etsy keeps it server-side), append
`listingdraft.panel_replaced` so the projection carries the new id. If deleting
the old image fails and removing the new one also fails, both panels are live:
`listingdraft.panel_orphaned` is recorded and the listing is never proposed again
(no re-upload over an unknown state) until reconciled by hand.
undo: re-attach the old id at its rank FIRST, then delete the new image (a 404 there
means already gone), so the listing never lacks a panel; then append the event
reversed. A listing with an undone action of this capability is not re-proposed.
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.panel import render_panel_jpeg
from shopsteward.pipeline.listings.backfill import PLANNED, _events, _snapshots
from shopsteward.pipeline.listings.digital_files import FilePlan, plan_sizes
from shopsteward.pipeline.listings.source_assets import resolve_source
from shopsteward.pipeline.ops.capabilities.size_claims_fix import _delivered_plan, _refresh
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.projections import action_rows
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id

KEY = "listing.panel_replace"
INTENT = "digital_whatyougot"
EVENT = "listingdraft.panel_replaced"
ORPHANED = "listingdraft.panel_orphaned"


def _recorded_sizes(conn: sqlite3.Connection, user_id: int, img: dict) -> list[str] | None:
    if img.get("panel_sizes") is not None:
        return img["panel_sizes"]
    row = conn.execute(
        "SELECT params_json FROM proj_mockups WHERE user_id=? AND path=? AND intent=?",
        (user_id, img["path"], INTENT),
    ).fetchone()
    return json.loads(row["params_json"] or "{}").get("sizes") if row else None


def _plan_for(
    conn: sqlite3.Connection, user_id: int, lid: int, row: sqlite3.Row, adapter: EtsyWriteAdapter
) -> FilePlan | None:
    if any(e.type == PLANNED for e in _events(conn, user_id, lid)):
        return _delivered_plan(conn, user_id, lid, adapter)
    if row["files_plan_json"] is None:
        return None
    try:
        return FilePlan.model_validate(
            {k: json.loads(row["files_plan_json"])[k] for k in ("source_dims", "focus", "files")}
        )
    except (ValueError, KeyError, TypeError):
        return None


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, adapter: EtsyWriteAdapter
) -> dict[str, ProposedAction]:
    _refresh(conn)
    rows: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute(
        "SELECT draft_id, etsy_listing_id, images_json, files_plan_json FROM proj_listing_drafts "
        "WHERE user_id=? AND etsy_listing_id IS NOT NULL AND state IN ('pushed','published') "
        "ORDER BY draft_id",
        (user_id,),
    ):
        rows.setdefault(int(r["etsy_listing_id"]), []).append(r)

    snaps = _snapshots(conn, user_id)
    skip = {  # operator-undone, or both panels live after a failed cleanup
        int(r["target_id"])
        for r in action_rows(conn)
        if r["user_id"] == user_id and r["capability"] == KEY and r["state"] == "undone"
    } | {
        int(e.payload["etsy_listing_id"]) for e in read_all(conn, ORPHANED) if e.user_id == user_id
    }
    today = datetime.now(UTC).date()
    cfg_hash = ops_config_hash(cfg)
    expires_at = (today + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
    out: dict[str, ProposedAction] = {}
    for lid, drafts in sorted(rows.items()):
        snap = snaps.get(lid)
        panels = [
            (r, i)
            for r in drafts
            for i in json.loads(r["images_json"] or "[]")
            if i.get("intent") == INTENT
        ]
        if lid in skip or snap is None or snap.state != "active" or len(panels) != 1:
            continue  # several/no panel entries: cannot be certain which image it is
        row, img = panels[0]
        if not img.get("etsy_image_id") or not isinstance(img.get("rank"), int):
            continue
        ref = resolve_source(conn, user_id, lid)
        if ref is None or not ref.on_disk_present:
            continue  # nothing to render from
        old_sizes = _recorded_sizes(conn, user_id, img)
        plan = _plan_for(conn, user_id, lid, row, adapter)
        if old_sizes is None or plan is None or old_sizes == plan_sizes(plan):
            continue
        new_sizes = plan_sizes(plan)
        inputs_hash = hashlib.sha256(
            f"{lid}|{img['etsy_image_id']}|{new_sizes}".encode()
        ).hexdigest()
        out[str(lid)] = ProposedAction(
            action_id=compute_action_id(KEY, str(lid), inputs_hash, cfg_hash, today.isoformat()),
            capability=KEY,
            target_type="listing",
            target_id=str(lid),
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=(
                f"{snap.title} -- gallery panel shows {', '.join(old_sizes) or 'no sizes'}; "
                f"files deliver {', '.join(new_sizes)}. Replace image {img['etsy_image_id']} "
                f"(rank {img['rank']}); undo re-attaches it."
            ),
            inputs_hash=inputs_hash,
            estimated_cost_usd=0.0,
            undo_available=True,
            expires_at=expires_at,
            params={
                "listing_id": lid,
                "draft_id": row["draft_id"],
                "old_image_id": img["etsy_image_id"],
                "rank": img["rank"],
                "old_sizes": old_sizes,
                "new_sizes": new_sizes,
            },
        )
    return out


def _swap_event(conn: sqlite3.Connection, user_id: int, p: dict, new_id: int, sizes: list) -> None:
    append(
        conn,
        Event(
            user_id=user_id,
            type=EVENT,
            payload={
                "draft_id": p["draft_id"],
                "etsy_listing_id": p["listing_id"],
                "rank": p["rank"],
                "etsy_image_id": new_id,
                "sizes": sizes,
            },
        ),
    )


class ListingPanelReplace:
    key = KEY
    max_tier = Tier.AUTO  # operator decision 2026-11-22 (module docstring)
    policy_verified = True  # listings_w image upload/delete, as the push stage already uses

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
        p = action.params
        lid, rank, old_id = int(action.target_id), p["rank"], p["old_image_id"]
        _refresh(conn)
        row = conn.execute(
            "SELECT images_json FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
            (user_id, p["draft_id"]),
        ).fetchone()
        current = (
            [i for i in json.loads(row["images_json"] or "[]") if i.get("intent") == INTENT]
            if row
            else []
        )
        if (
            len(current) != 1
            or current[0].get("etsy_image_id") != old_id
            or current[0]["rank"] != rank
        ):
            raise StaleTargetError(f"listing {lid}: panel image changed since proposal -- refusing")
        ref = resolve_source(conn, user_id, lid)
        if ref is None or not ref.on_disk_present or ref.on_disk_path is None:
            raise ValueError(f"listing {lid}: source photo not on disk -- cannot render the panel")

        new = self._adapter.upload_listing_image(
            lid, render_panel_jpeg(ref.on_disk_path, p["new_sizes"]), rank=rank
        )
        try:
            self._adapter.delete_listing_image(lid, old_id)
        except EtsyWriteError as exc:
            try:
                self._adapter.delete_listing_image(lid, new.listing_image_id)  # no duplicate
            except EtsyWriteError:
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type=ORPHANED,
                        payload={
                            "draft_id": p["draft_id"],
                            "etsy_listing_id": lid,
                            "rank": rank,
                            "old_image_id": old_id,
                            "new_image_id": new.listing_image_id,
                        },
                    ),
                )
                raise StaleTargetError(
                    f"listing {lid}: old panel {old_id} and new panel {new.listing_image_id} "
                    "are BOTH live; reconcile by hand"
                ) from exc
            if exc.status_code == 404:
                raise StaleTargetError(f"listing {lid}: image {old_id} is gone on Etsy") from exc
            raise
        _swap_event(conn, user_id, p, new.listing_image_id, p["new_sizes"])
        return ExecutionResult(
            before={"image_id": old_id, "rank": rank, "sizes": p["old_sizes"]},
            after={"image_id": new.listing_image_id, "rank": rank, "sizes": p["new_sizes"]},
            cost_usd=0.0,
            duration_ms=0,
        )

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        done = None
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                done = e.payload
        if done is None:
            return
        lid, old, new = int(action.target_id), done["before"], done["after"]
        try:  # old first: a failure here leaves the new panel in place, never none
            self._adapter.attach_listing_image(lid, old["image_id"], rank=old["rank"])
        except EtsyWriteError as exc:
            if exc.status_code == 404:
                raise StaleTargetError(
                    f"listing {lid}: old panel image gone on Etsy -- no undo"
                ) from exc
            raise
        try:
            self._adapter.delete_listing_image(lid, new["image_id"])
        except EtsyWriteError as exc:
            if exc.status_code != 404:  # 404 = already gone: a retried undo completes
                raise
        _swap_event(conn, user_id, action.params, old["image_id"], old["sizes"])

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
