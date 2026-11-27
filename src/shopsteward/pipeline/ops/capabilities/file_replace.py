"""`listing.file_replace` -- swaps the rank-1 (full-frame) digital file on a live
digital listing for an operator-supplied, re-exported JPEG (e.g. sensor dust
healed in Lightroom). Ranks 2-5 (the multi-ratio crops) are never touched.

TIER: `Tier.AUTO` (operator, 2026-11-27) -- but PER ACTION, and only with a real
undo. Etsy's API cannot download a listing file (the adapter has no such call)
and a deleted file cannot be re-attached by id (that exists for images only), so
the undo cannot come from Etsy. It re-uploads the OLD file's bytes, which must
exist locally BEFORE the delete:
- the old rank-1 file's sha256 must be recorded in the event log
  (`listingdraft.file_attached` for pipeline-made listings, or this capability's
  own `listingfile.replaced` for an earlier swap), and
- the bytes must be reproducible: re-encoded (`_full_file`) or read verbatim
  (`sellable_file_bytes`) from the listing's on-disk or archived master, and the
  result must hash to that recorded sha256. The match is the proof, not a guess.
- the verified bytes are saved under `{asset_store.root}/replaced_files/
  {action_id}.jpg` before anything is deleted.
Undo uploads those bytes as rank 1 under the old name and deletes the new file,
through the same 5-file-cap ordering as `execute()` (`_swap`).

Anything that cannot meet that bar stays operator-approved: `build_action` sets
`params["auto_hold"]` to the reason (also prefixed to the proposal reason, so it
shows under NEEDS YOU) and `undo_available=False`, and the runner clamps such an
action to PROPOSE (fresh or re-driven). Held cases: no recorded sha (adopted or
hand-uploaded listings, which is most already-live ones), no reproducible
source, no asset-store location, a resume (old file already deleted), or a
same-photo failure: the new file's pHash must be within
`catalog_expansion.dedup_max_distance` of the verified old file's. The operator
can still `ops approve` a held action (no undo then). `execute()` re-checks all
of this for an un-held action and refuses (non-terminal) if it no longer holds.

Trigger: `ops replace-file <listing_id> --file PATH` (a direct action like
`ops edit-listing`): `build_action` validates and the runner records it, and
executes it at once if the effective tier is AUTO and nothing holds it.
`propose()` is always `[]`: nothing is ever proposed unprompted.

Encoding is the pipeline's rank-1 path: `plan_files(dims, ...)` full entry ->
`encode_file` (sRGB, EXIF, `digital_files.max_bytes` <= 20 MB). The file keeps
the old rank-1 file's name (buyers see the same name; backfill's "exactly one
legacy file" rule still holds on adopted listings).

Order depends on Etsy's 5-file cap (`_swap`):
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
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.backfill import BackfillRefused, check_preconditions
from shopsteward.pipeline.listings.digital_files import (
    DigitalFilesConfig,
    encode_file,
    photo_dims,
    plan_files,
)
from shopsteward.pipeline.listings.images import sellable_file_bytes
from shopsteward.pipeline.listings.photo_match import hamming_distance, phash_bytes
from shopsteward.pipeline.listings.push import _file_name, _slug
from shopsteward.pipeline.listings.source_assets import resolve_source
from shopsteward.pipeline.ops.capabilities.size_claims_fix import _refresh
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id

KEY = "listing.file_replace"
OLD_DELETED = "listingfile.replace_old_deleted"
REPLACED = "listingfile.replaced"
ORPHANED = "listingfile.replace_orphaned"
UNDONE = "listingfile.replace_undone"  # in _events(): a finished undo closes any OLD_DELETED


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
            for t in (OLD_DELETED, REPLACED, ORPHANED, UNDONE)
            for e in read_all(conn, t)
            if e.user_id == user_id and int(e.payload["etsy_listing_id"]) == lid
        ),
        key=lambda e: e.id or 0,
    )


def _recorded_sha(conn: sqlite3.Connection, user_id: int, lid: int, file_id: int) -> str | None:
    """The sha256 the event log recorded for this Etsy file id (None: unknown)."""
    sha: str | None = None
    for etype, id_key, sha_key in (
        ("listingdraft.file_attached", "etsy_file_id", "sha256"),
        (REPLACED, "new_file_id", "new_sha256"),
        (UNDONE, "restored_file_id", "restored_sha256"),
    ):
        for e in read_all(conn, etype):
            p = e.payload
            if (
                e.user_id == user_id
                and str(p.get("etsy_listing_id")) == str(lid)
                and p.get(id_key) == file_id
                and p.get(sha_key)
            ):
                sha = str(p[sha_key])
    return sha


def _source_paths(conn: sqlite3.Connection, user_id: int, lid: int) -> list[str]:
    ref = resolve_source(conn, user_id, lid)
    if ref is None:
        return []
    paths = [ref.on_disk_path] if ref.on_disk_present and ref.on_disk_path else []
    if ref.photo_id is not None:
        rows = conn.execute(
            "SELECT stored_key FROM proj_asset_store WHERE user_id=? AND photo_id=?",
            (user_id, ref.photo_id),
        ).fetchall()
        if rows:
            root = asset_store_config.resolve_root(
                asset_store_config.get_asset_store_config(conn, user_id)
            )
            paths += [
                str(root / r["stored_key"]) for r in rows if (root / r["stored_key"]).exists()
            ]
    return paths


def _write_durable(path: Path, data: bytes) -> None:
    """Write + fsync the file, then fsync its directory (best effort: not
    supported on Windows), so the copy survives a crash right after the delete."""
    with path.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stash_path(conn: sqlite3.Connection, user_id: int, action_id: str) -> Path:
    root = asset_store_config.resolve_root(asset_store_config.get_asset_store_config(conn, user_id))
    return root / "replaced_files" / f"{action_id}.jpg"


def _assess(
    conn: sqlite3.Connection,
    user_id: int,
    lid: int,
    old_id: int,
    new_data: bytes,
    dcfg: DigitalFilesConfig,
    cfg: OpsConfig,
) -> tuple[str | None, bytes | None]:
    """(hold reason, verified old bytes). Reason None means an AUTO run is safe:
    the old bytes are reproduced and sha-verified, a stash location exists, and
    the new file is the same photo. The bytes are returned even when only the
    pHash check failed, so an operator-approved held action still stashes them."""
    if not old_id:
        return "resume: the old file is already deleted, nothing to preserve", None
    want = _recorded_sha(conn, user_id, lid, old_id)
    if want is None:
        return "no recorded sha256 for the old rank-1 file (adopted/hand-uploaded)", None
    max_bytes = listing_config.get_config(conn, user_id).etsy.sellable_max_bytes
    old: bytes | None = None
    try:
        _stash_path(conn, user_id, "probe")  # the asset store must be configured
        for path in _source_paths(conn, user_id, lid):
            makers: tuple[Callable[[str], bytes], ...] = (
                lambda p: _full_file(p, dcfg)[0],
                lambda p: sellable_file_bytes(p, max_bytes)[0],
            )
            for make in makers:
                try:
                    data = make(path)
                except (OSError, ValueError):
                    continue
                if hashlib.sha256(data).hexdigest() == want:
                    old = data
                    break
            if old is not None:
                break
    except (KeyError, LookupError) as exc:
        return f"no asset-store location for the undo copy ({exc})", None
    if old is None:
        return "old rank-1 bytes are not reproducible from the archived master", None
    try:
        dist = hamming_distance(phash_bytes(new_data), phash_bytes(old))
    except ValueError:
        return "pHash could not decode the old or new file", old
    limit = cfg.catalog_expansion.dedup_max_distance
    if dist > limit:
        return f"not the same photo: pHash distance {dist} > {limit}", old
    return None, old


ETSY_MAX_FILES = 5  # Etsy's hard per-listing cap (not the configurable digital_files.max_files)


def _target(files: list, evs: list[Event]) -> tuple[int, str | None]:
    """(old_file_id, name) to replace; old_file_id 0 = resume (upload only).
    Resume is keyed on FILE ID, checked before any rank lookup: if the newest event
    is `replace_old_deleted` and that id is gone, a crop that Etsy renumbered to
    rank 1 must never be taken for the full frame."""
    if evs and evs[-1].type == OLD_DELETED:
        pend = evs[-1].payload
        if int(pend["old_file_id"]) not in {f.listing_file_id for f in files}:
            return 0, pend.get("name")
    rank1 = [f for f in files if f.rank == 1]
    if len(rank1) != 1:
        raise ValueError(f"expected exactly one rank-1 file, found {len(rank1)}")
    return rank1[0].listing_file_id, rank1[0].name


class ListingFileReplace:
    key = KEY
    max_tier = Tier.AUTO  # module docstring: AUTO only per action, and only with a verified undo
    policy_verified = True  # listings_w file upload/delete, as backfill already uses

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
            old_id, name = _target(self._adapter.list_listing_files(listing_id), evs)
            new_data, sha = _full_file(path, dcfg)
        except (EtsyWriteError, OSError, ValueError) as exc:
            raise ValueError(f"listing {listing_id}: {exc}") from exc
        name = name or _file_name(
            _slug(path), plan_files(photo_dims(path), (0.5, 0.5), dcfg).files[0]
        )
        hold, _ = _assess(conn, user_id, listing_id, old_id, new_data, dcfg, cfg)
        today_date = today or datetime.now(UTC).date()
        inputs_hash = hashlib.sha256(f"{listing_id}|{old_id}|{sha}".encode()).hexdigest()
        why = (
            reason or f"replace rank-1 file {old_id or '(already deleted)'} with {Path(path).name}"
        )
        return ProposedAction(
            action_id=compute_action_id(
                KEY, str(listing_id), inputs_hash, ops_config_hash(cfg), today_date.isoformat()
            ),
            capability=KEY,
            target_type="listing",
            target_id=str(listing_id),
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=f"HELD for operator ({hold}): {why}" if hold else why,
            inputs_hash=inputs_hash,
            estimated_cost_usd=0.0,
            undo_available=hold is None,
            expires_at=(today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat(),
            params={
                "listing_id": listing_id,
                "old_file_id": old_id,
                "name": name,
                "path": path,
                "sha256": sha,
                "auto_hold": hold or "",  # runner: non-empty clamps this action to PROPOSE
            },
        )

    def _swap(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        lid: int,
        *,
        remove_id: int,
        data: bytes,
        name: str,
        files: list,
        marker: dict | None = None,
    ) -> int:
        """Put `data` in as rank 1 and take `remove_id` out; returns the new file
        id. Shared by execute() and undo() so the 5-file-cap ordering, resume
        marker and orphan handling are one implementation."""
        # At Etsy's cap there is no room to upload first. Never delete the last file.
        if remove_id and len(files) >= ETSY_MAX_FILES and len(files) > 1:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type=OLD_DELETED,
                    payload={
                        "etsy_listing_id": lid,
                        "old_file_id": remove_id,
                        "name": name,
                        **(marker or {}),
                    },
                ),
            )
            self._adapter.delete_listing_file(lid, remove_id)
            # a failure here leaves no rank-1 file: re-run `ops replace-file` to resume
            return self._adapter.upload_listing_file(lid, data, name=name, rank=1).listing_file_id

        new = self._adapter.upload_listing_file(lid, data, name=name, rank=1)
        if remove_id:
            try:
                self._adapter.delete_listing_file(lid, remove_id)
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
                                "old_file_id": remove_id,
                                "new_file_id": new.listing_file_id,
                            },
                        ),
                    )
                    raise StaleTargetError(
                        f"listing {lid}: old file {remove_id} and new file {new.listing_file_id} "
                        "are BOTH live; reconcile by hand"
                    ) from exc
                raise
        return new.listing_file_id

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        from shopsteward.pipeline.ops.config import get_ops_config

        p = action.params
        lid, old_id, name = int(action.target_id), int(p["old_file_id"]), str(p["name"])
        dcfg = _dcfg(conn, user_id)
        data, sha = _full_file(str(p["path"]), dcfg)
        if sha != p["sha256"]:
            raise StaleTargetError(f"listing {lid}: replacement file changed since proposal")
        files = self._adapter.list_listing_files(lid)
        evs = _events(conn, user_id, lid)
        try:
            current = _target(files, evs)[0]
        except ValueError as exc:
            raise StaleTargetError(f"listing {lid}: {exc} -- refusing") from exc
        if current != old_id:
            raise StaleTargetError(f"listing {lid}: rank-1 file changed since proposal -- refusing")
        if any(e.type == ORPHANED for e in evs):
            raise StaleTargetError(f"listing {lid}: orphaned; reconcile by hand")

        # Undo copy BEFORE any delete. An un-held (auto-eligible) action must still
        # have it; a held one (operator-approved) saves it if it can, else has no undo.
        hold, old_bytes = _assess(
            conn, user_id, lid, old_id, data, dcfg, get_ops_config(conn, user_id)
        )
        if hold and not p.get("auto_hold"):
            # not StaleTargetError: a refusal stays proposed (runner), never terminal
            raise ValueError(f"listing {lid}: undo no longer guaranteed ({hold}); not executing")
        old_sha: str | None = None
        if old_bytes is not None:
            stash = _stash_path(conn, user_id, action.action_id)
            stash.parent.mkdir(parents=True, exist_ok=True)
            _write_durable(stash, old_bytes)
            old_sha = hashlib.sha256(old_bytes).hexdigest()

        # the at-cap delete marker records the stash, so a crash before REPLACED still
        # leaves a durable pointer to the undo copy (replaced_files/<action_id>.jpg)
        marker = {"action_id": action.action_id, "old_sha256": old_sha} if old_sha else None
        new_id = self._swap(
            conn, user_id, lid, remove_id=old_id, data=data, name=name, files=files, marker=marker
        )
        append(
            conn,
            Event(
                user_id=user_id,
                type=REPLACED,
                payload={
                    "etsy_listing_id": lid,
                    "old_file_id": old_id,
                    "new_file_id": new_id,
                    "action_id": action.action_id,
                    "name": name,
                    "old_sha256": old_sha,
                    "new_sha256": sha,
                },
            ),
        )
        return ExecutionResult(
            before={"file_id": old_id, "name": name},
            after={"file_id": new_id, "name": name, "sha256": sha},
            cost_usd=0.0,
            duration_ms=0,
        )

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        """Re-upload the stashed old bytes as rank 1 under the old name, then
        remove the replacement. Refuses (raises, nothing written) if the stash is
        missing or no longer hashes to what was recorded."""
        lid = int(action.target_id)
        rep = next(
            (
                e.payload
                for e in reversed(read_all(conn, REPLACED))
                if e.user_id == user_id and e.payload.get("action_id") == action.action_id
            ),
            None,
        )
        if rep is None or not rep.get("old_sha256"):
            raise ValueError(f"{action.action_id}: no undo copy was kept (held action)")
        stash = _stash_path(conn, user_id, action.action_id)
        old_bytes = stash.read_bytes()
        if hashlib.sha256(old_bytes).hexdigest() != rep["old_sha256"]:
            raise ValueError(f"{action.action_id}: undo copy is corrupt (sha256 mismatch)")
        files = self._adapter.list_listing_files(lid)
        new_id = int(rep["new_file_id"])
        if new_id not in {f.listing_file_id for f in files}:
            raise ValueError(f"listing {lid}: replacement file {new_id} is gone; cannot undo")
        restored = self._swap(
            conn,
            user_id,
            lid,
            remove_id=new_id,
            data=old_bytes,
            name=str(rep["name"]),
            files=files,
            marker={"action_id": action.action_id, "old_sha256": rep["old_sha256"]},
        )
        append(
            conn,
            Event(
                user_id=user_id,
                type=UNDONE,
                payload={
                    "etsy_listing_id": lid,
                    "action_id": action.action_id,
                    "removed_file_id": new_id,
                    "restored_file_id": restored,
                    "restored_sha256": rep["old_sha256"],
                },
            ),
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
