"""`archive adopt-local` (design: source-photo-match backfill): match local
archive photo files against real Etsy listing images via perceptual hash
(photo_match.py), then backfill the source-asset linkage for manual/
pre-pipeline listings that source_assets.resolve_source() currently can't
resolve. Operator-invoked CLI only -- no autonomous execution, dry-run by
default.

Only listings with NO existing proj_listing_drafts row are ever considered
(resolve_source() returns None for those -- "manual/pre-pipeline listing").
A listing the pipeline already built/pushed a real draft for is left alone;
matching it here would risk a second, confusing proj_listing_drafts row for
the same etsy_listing_id. This also makes --apply idempotent for free: once
adopted, the listing has a draft row and drops out of future matching runs.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from shopsteward.adapters.etsy.interface import EtsyAdapter
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.listings import archive
from shopsteward.pipeline.listings.models import (
    AdoptReport,
    AssetStoreConfig,
    MatchConfig,
    MatchResult,
)
from shopsteward.pipeline.listings.photo_match import hamming_distance, phash_bytes
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.source_assets import resolve_source

_JPG_SUFFIXES = (".jpg", ".jpeg")


@dataclass
class _Ranked:
    path: Path
    distance: int


def scan_local_files(folder: Path, recursive: bool) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in _JPG_SUFFIXES)


def hash_local_files(paths: list[Path]) -> dict[Path, int]:
    return {p: phash_bytes(p.read_bytes()) for p in paths}


def classify(
    listing_hash: int, local_hashes: dict[Path, int], cfg: MatchConfig
) -> tuple[Path | None, int | None, str]:
    """Returns (best_path, best_distance, verdict). Never guesses past
    match/ambiguous/unmatched (design invariant)."""
    if not local_hashes:
        return None, None, "unmatched"

    ranked = sorted(
        (_Ranked(p, hamming_distance(listing_hash, h)) for p, h in local_hashes.items()),
        key=lambda r: r.distance,
    )
    best = ranked[0]
    if best.distance > cfg.max_distance:
        return None, None, "unmatched"

    if len(ranked) > 1:
        margin = ranked[1].distance - best.distance
        if margin < cfg.min_margin:
            return best.path, best.distance, "ambiguous"

    return best.path, best.distance, "match"


def _revoked(conn: sqlite3.Connection, user_id: int, listing_id: int) -> bool:
    """True iff the most recent listing.source_* event for this listing_id is
    a revoke. Durability for revoke(): resolve_source() alone isn't enough --
    it returns None right after a revoke (no draft row) for the SAME reason
    it returns None for a never-adopted listing, and plan_matches would
    otherwise treat the two as identical and immediately re-adopt the exact
    match the operator just undid. Events are the ground truth for "was this
    undone", since the revoke never removes the original source_adopted
    event, only outranks it by being newer."""
    events = read_all(conn, "listing.source_")
    for event in reversed(events):
        if event.user_id == user_id and event.payload.get("etsy_listing_id") == listing_id:
            return event.type == "listing.source_match_revoked"
    return False


def plan_matches(
    conn: sqlite3.Connection,
    user_id: int,
    etsy_adapter: EtsyAdapter,
    folder: Path,
    *,
    recursive: bool,
    cfg: MatchConfig,
) -> list[MatchResult]:
    """Read-only: fetches listing images, hashes local candidates, classifies.
    Never writes an event or touches the archive -- used for both the dry-run
    table and to decide what --apply actually adopts."""
    local_hashes = hash_local_files(scan_local_files(folder, recursive))

    results: list[MatchResult] = []
    for listing in etsy_adapter.list_listings():
        if resolve_source(conn, user_id, listing.listing_id) is not None:
            continue  # already linked by the real pipeline or a prior adopt
        if _revoked(conn, user_id, listing.listing_id):
            continue  # operator explicitly undid a prior adopt -- stays
            # excluded from matching permanently; --pin is the only override.

        images = etsy_adapter.get_listing_images(listing.listing_id)
        if not images:
            results.append(
                MatchResult(
                    listing_id=listing.listing_id,
                    local_path=None,
                    distance=None,
                    verdict="unmatched",
                )
            )
            continue

        primary = min(images, key=lambda i: i.rank)
        listing_hash = phash_bytes(etsy_adapter.download_image(primary.url_570xN))
        path, distance, verdict = classify(listing_hash, local_hashes, cfg)
        results.append(
            MatchResult(
                listing_id=listing.listing_id,
                local_path=str(path) if path is not None else None,
                distance=distance,
                verdict=verdict,
            )
        )
    return results


def _already_adopted(conn: sqlite3.Connection, user_id: int, listing_id: int) -> bool:
    return resolve_source(conn, user_id, listing_id) is not None


def _ingest_matched_file(conn: sqlite3.Connection, user_id: int, path: Path) -> str | None:
    """Landing registration scoped to exactly this ONE matched file.
    landing.scan_landing() scans its whole containing folder -- calling it
    here would silently enroll every bystander photo in an archive folder
    into the Etsy landing pipeline (CLAUDE.md: "the landing folder is the
    only Etsy handoff"; an arbitrary archive folder must never be promoted
    to that status just because one file in it got matched). Reuses
    landing.py's own per-file validate/match helpers so this can never
    silently drift from scan_landing's rules -- it just runs them for one
    path instead of a directory listing. Returns the file's sha256 id, or
    None if the file failed validation (e.g. below min_long_edge_px) --
    mirrors scan_landing's own "reason" in result signal, which the caller
    must treat as a hard stop rather than proceeding to archive/adopt."""
    from shopsteward.editing.projections import rebuild_editing
    from shopsteward.pipeline.landing import (
        _SUFFIX_FORMATS,
        _known_base_names,
        _known_file_ids,
        _match_photo,
        _sha256_file,
        _validate,
    )
    from shopsteward.pipeline.projections import rebuild_pipeline

    file_id = _sha256_file(path)
    if file_id in _known_file_ids(conn, user_id):
        return file_id  # already observed (e.g. a prior adopt run) -- no-op

    rebuild_editing(conn)  # keep proj_photos fresh for base_name matching
    landing_cfg = tuning.get_profile(conn, user_id).landing
    fmt = _SUFFIX_FORMATS.get(path.suffix.lower())
    result = (
        {"reason": "unsupported_format"}
        if fmt is None
        else _validate(
            path,
            fmt=fmt,
            allowed_formats=landing_cfg.allowed_formats,
            min_long_edge_px=landing_cfg.min_long_edge_px,
        )
    )

    if "reason" in result:
        append(
            conn,
            Event(
                user_id=user_id,
                type="landing.file_invalid",
                payload={"file_id": file_id, "path": str(path), "reason": result["reason"]},
            ),
        )
        rebuild_pipeline(conn)
        return None
    else:
        base_name, photo_id = _match_photo(path.stem, _known_base_names(conn, user_id))
        append(
            conn,
            Event(
                user_id=user_id,
                type="landing.file_observed",
                payload={
                    "file_id": file_id,
                    "path": str(path),
                    "base_name": base_name,
                    "format": fmt,
                    "width": result["width"],
                    "height": result["height"],
                    "color_space": result["color_space"],
                    "photo_id": photo_id,
                },
            ),
        )
    rebuild_pipeline(conn)
    return file_id


def adopt_one(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: AssetStoreConfig,
    *,
    listing_id: int,
    local_path: str,
    distance: int | None,
    match_source: str,
) -> bool:
    """Backfills the linkage for one confirmed match: registers just this
    one file as a landing file (not its whole containing folder --
    _ingest_matched_file), archive_master() the untouched original, then
    append listing.source_adopted. Idempotent -- a no-op if this listing_id
    already resolves (real draft or a prior adopt). Also a no-op -- no
    archive, no listing.source_adopted -- if the matched file fails landing
    validation (e.g. below min_long_edge_px): a phash match on a low-res
    photo must never become eligible for a real reprint decision downstream
    (gapfill.py gates only on source.archived existing)."""
    if _already_adopted(conn, user_id, listing_id):
        return False

    path = Path(local_path)
    tuning.seed(conn, user_id, TUNING_PROFILE_PATH)
    file_id = _ingest_matched_file(conn, user_id, path)
    if file_id is None:
        return False
    photo_id = f"file-{file_id[:12]}"

    with Image.open(path) as img:
        width, height = img.size

    archive.archive_master(
        conn,
        user_id,
        cfg,
        photo_id=photo_id,
        source_landing_file_id=file_id,
        path=str(path),
        format="JPEG",
        width=width,
        height=height,
    )

    append(
        conn,
        Event(
            user_id=user_id,
            type="listing.source_adopted",
            payload={
                "draft_id": f"adopted-{listing_id}",
                "etsy_listing_id": listing_id,
                "photo_id": photo_id,
                "landing_file_id": file_id,
                "match_distance": distance,
                "match_source": match_source,
            },
        ),
    )
    rebuild_listings(conn)
    return True


def revoke(conn: sqlite3.Connection, user_id: int, listing_id: int) -> bool:
    """Undo path: appends a NEW event recording the correction (the old
    listing.source_adopted event is never touched -- events are immutable);
    only the derived proj_listing_drafts row is removed on rebuild.

    No-op (returns False, appends nothing) if this listing_id was never
    adopted in the first place -- per _revoked()'s own durability logic, a
    revoke event permanently excludes a listing_id from future auto-matching
    (only --pin overrides it), so revoking a listing that was never adopted
    would wrongly blacklist it forever. resolve_source() is the same check
    plan_matches() already uses to decide "was this ever adopted"."""
    if resolve_source(conn, user_id, listing_id) is None:
        return False
    append(
        conn,
        Event(
            user_id=user_id,
            type="listing.source_match_revoked",
            payload={"etsy_listing_id": listing_id},
        ),
    )
    rebuild_listings(conn)
    return True


def apply_matches(
    conn: sqlite3.Connection, user_id: int, cfg: AssetStoreConfig, results: list[MatchResult]
) -> AdoptReport:
    """Adopts every `match` verdict in `results`. Never adopts `ambiguous`/
    `unmatched` -- those need an explicit --pin."""
    report = AdoptReport()
    for r in results:
        if r.verdict == "match":
            report.matched += 1
            assert r.local_path is not None
            if adopt_one(
                conn,
                user_id,
                cfg,
                listing_id=r.listing_id,
                local_path=r.local_path,
                distance=r.distance,
                match_source="phash",
            ):
                report.adopted += 1
        elif r.verdict == "ambiguous":
            report.ambiguous += 1
        elif r.verdict == "unmatched":
            report.unmatched += 1
    return report
