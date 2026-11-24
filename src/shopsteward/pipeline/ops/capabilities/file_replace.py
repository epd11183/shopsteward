"""`listing.file_replace` -- swaps the rank-1 (full-frame) digital file on a live
digital listing for an operator-supplied, re-exported JPEG (e.g. sensor dust
healed in Lightroom). Ranks 2-5 (the multi-ratio crops) are never touched.

TIER: `Tier.PROPOSE`, `undo = None` (registry invariant 1). Nothing in this repo
(docs, fixtures, adapter comments) shows that Etsy keeps a deleted LISTING FILE
server-side so it can be re-attached by id (the repo only records that for
images), and the adapter has no attach-file call. Without a proven re-attach
there is no honest undo, so this stays operator-approved. If a live probe proves
`uploadListingFile` with `listing_file_id` re-attaches a deleted file, add
`attach_listing_file` to the adapter and lift this to AUTO with a real undo.

Trigger: `ops replace-file <listing_id> --file PATH` (a direct action like
`ops edit-listing`): `build_action` validates, the runner records it as a
proposal, the operator runs `ops approve <id> --live-autonomy`. `propose()` is
always `[]`: nothing is ever proposed unprompted.

Encoding is the pipeline's rank-1 path: `plan_files(dims, ...)` full entry ->
`encode_file` (sRGB, EXIF, `digital_files.max_bytes` <= 20 MB). The file keeps
the old rank-1 file's name (buyers see the same name; backfill's "exactly one
legacy file" rule still holds on adopted listings).

Order depends on Etsy's 5-file cap:
- under the cap: upload the new file, THEN delete the old one (a listing's only
  file must never be deleted first: Etsy would convert it to physical). If the
  delete fails and the new file cannot be removed either, both are live:
  `listingfile.replace_orphaned` is recorded and the listing is refused from
  then on until reconciled by hand.
- at the cap (the usual backfilled listing): delete first, then upload.
  `listingfile.replace_old_deleted` is appended BEFORE the delete, so if the
  upload then fails the listing has no rank-1 file and a re-run of
  `ops replace-file` resumes (uploads only) instead of refusing.
"""

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.backfill import BackfillRefused, check_preconditions
from shopsteward.pipeline.listings.digital_files import (
    DigitalFilesConfig,
    encode_file,
    photo_dims,
    plan_files,
)
from shopsteward.pipeline.listings.push import _file_name, _slug
from shopsteward.pipeline.ops.capabilities.size_claims_fix import _refresh
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id

KEY = "listing.file_replace"
OLD_DELETED = "listingfile.replace_old_deleted"
REPLACED = "listingfile.replaced"
ORPHANED = "listingfile.replace_orphaned"


def _dcfg(conn: sqlite3.Connection, user_id: int) -> DigitalFilesConfig:
    _refresh(conn)
    try:
        return check_preconditions(listing_config.get_config(conn, user_id), live=True)
    except BackfillRefused as exc:
        raise ValueError(str(exc)) from exc


def _full_file(path: str, dcfg: DigitalFilesConfig) -> tuple[bytes, str]:
    """The exact bytes the pipeline would ship as rank 1 for this image."""
    full = plan_files(photo_dims(path), (0.5, 0.5), dcfg).files[0]
    return encode_file(path, full, dcfg)


def _events(conn: sqlite3.Connection, user_id: int, lid: int) -> list[Event]:
    return sorted(
        (
            e
            for t in (OLD_DELETED, REPLACED, ORPHANED)
            for e in read_all(conn, t)
            if e.user_id == user_id and int(e.payload["etsy_listing_id"]) == lid
        ),
        key=lambda e: e.id or 0,
    )


def _rank1(adapter: EtsyWriteAdapter, lid: int) -> tuple[int, list]:
    files = adapter.list_listing_files(lid)
    return len(files), [f for f in files if f.rank == 1]


class ListingFileReplace:
    key = KEY
    max_tier = Tier.PROPOSE  # module docstring: no proven re-attach, so no undo
    policy_verified = True  # listings_w file upload/delete, as backfill already uses
    undo = None

    def __init__(self, adapter: EtsyWriteAdapter) -> None:
        self._adapter = adapter

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return []

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        return None  # operator-directed only

    def build_action(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        cfg: OpsConfig,
        listing_id: int,
        file: str | Path,
        *,
        reason: str | None = None,
        today: date | None = None,
    ) -> ProposedAction:
        """Validate and build the action; ValueError (nothing written) if the
        listing or file is not eligible."""
        path = str(file)
        evs = _events(conn, user_id, listing_id)
        if any(e.type == ORPHANED for e in evs):
            raise ValueError(
                f"listing {listing_id}: old and new files both live; reconcile by hand"
            )
        rows = conn.execute(
            "SELECT provider, listing_type FROM proj_listing_drafts WHERE user_id=? "
            "AND etsy_listing_id IS NOT NULL AND CAST(etsy_listing_id AS INTEGER)=?",
            (user_id, listing_id),
        ).fetchall()
        if not rows or any(
            r["provider"] != "etsy_digital" or r["listing_type"] not in (None, "download")
            for r in rows
        ):
            raise ValueError(f"listing {listing_id}: not a known digital listing")
        dcfg = _dcfg(conn, user_id)
        try:
            _, rank1 = _rank1(self._adapter, listing_id)
            data, sha = _full_file(path, dcfg)
        except (EtsyWriteError, OSError, ValueError) as exc:
            raise ValueError(f"listing {listing_id}: {exc}") from exc
        del data
        if len(rank1) == 1:
            old_id, name = rank1[0].listing_file_id, rank1[0].name
        elif not rank1 and evs and evs[-1].type == OLD_DELETED:
            old_id, name = 0, evs[-1].payload.get("name")  # resume: old already deleted
        else:
            raise ValueError(
                f"listing {listing_id}: expected exactly one rank-1 file, found {len(rank1)}"
            )
        name = name or _file_name(
            _slug(path), plan_files(photo_dims(path), (0.5, 0.5), dcfg).files[0]
        )
        today_date = today or datetime.now(UTC).date()
        inputs_hash = hashlib.sha256(f"{listing_id}|{old_id}|{sha}".encode()).hexdigest()
        return ProposedAction(
            action_id=compute_action_id(
                KEY, str(listing_id), inputs_hash, ops_config_hash(cfg), today_date.isoformat()
            ),
            capability=KEY,
            target_type="listing",
            target_id=str(listing_id),
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=reason
            or f"replace rank-1 file {old_id or '(already deleted)'} with {Path(path).name}",
            inputs_hash=inputs_hash,
            estimated_cost_usd=0.0,
            undo_available=False,
            expires_at=(today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat(),
            params={
                "listing_id": listing_id,
                "old_file_id": old_id,
                "name": name,
                "path": path,
                "sha256": sha,
            },
        )

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        p = action.params
        lid, old_id, name = int(action.target_id), int(p["old_file_id"]), str(p["name"])
        dcfg = _dcfg(conn, user_id)
        data, sha = _full_file(str(p["path"]), dcfg)
        if sha != p["sha256"]:
            raise StaleTargetError(f"listing {lid}: replacement file changed since proposal")
        n_files, rank1 = _rank1(self._adapter, lid)
        if [f.listing_file_id for f in rank1] != ([old_id] if old_id else []):
            raise StaleTargetError(f"listing {lid}: rank-1 file changed since proposal -- refusing")
        evs = _events(conn, user_id, lid)
        if any(e.type == ORPHANED for e in evs) or (
            not old_id and not (evs and evs[-1].type == OLD_DELETED)
        ):
            raise StaleTargetError(f"listing {lid}: not eligible (orphaned, or nothing to resume)")

        def done(new_id: int) -> ExecutionResult:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type=REPLACED,
                    payload={"etsy_listing_id": lid, "old_file_id": old_id, "new_file_id": new_id},
                ),
            )
            return ExecutionResult(
                before={"file_id": old_id, "name": name},
                after={"file_id": new_id, "name": name, "sha256": sha},
                cost_usd=0.0,
                duration_ms=0,
            )

        if old_id and n_files >= dcfg.max_files:  # at the cap: no room to upload first
            append(
                conn,
                Event(
                    user_id=user_id,
                    type=OLD_DELETED,
                    payload={"etsy_listing_id": lid, "old_file_id": old_id, "name": name},
                ),
            )
            self._adapter.delete_listing_file(lid, old_id)
            # a failure here leaves no rank-1 file: re-run `ops replace-file` to resume
            return done(
                self._adapter.upload_listing_file(lid, data, name=name, rank=1).listing_file_id
            )

        new = self._adapter.upload_listing_file(lid, data, name=name, rank=1)
        if old_id:
            try:
                self._adapter.delete_listing_file(lid, old_id)
            except EtsyWriteError as exc:
                try:
                    self._adapter.delete_listing_file(lid, new.listing_file_id)  # no duplicate
                except EtsyWriteError:
                    append(
                        conn,
                        Event(
                            user_id=user_id,
                            type=ORPHANED,
                            payload={
                                "etsy_listing_id": lid,
                                "old_file_id": old_id,
                                "new_file_id": new.listing_file_id,
                            },
                        ),
                    )
                    raise StaleTargetError(
                        f"listing {lid}: old file {old_id} and new file {new.listing_file_id} "
                        "are BOTH live; reconcile by hand"
                    ) from exc
                raise
        return done(new.listing_file_id)

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
