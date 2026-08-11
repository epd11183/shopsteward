"""Print-file resolution + hosting (design §7.4). The print master is the
landing artifact `pod.print_file.prefer` names -- "tiff_master" prefers a
TIFF-format landing row (the Gate 2 export's AdobeRGB master); anything else
(e.g. "jpeg") prefers the sRGB JPEG export. Both artifacts land as SEPARATE
rows in proj_landing_files sharing one base_name/photo_id (pipeline/landing.py),
so resolving "the other one" is a sibling lookup, not a field on the row
itself.

Resolution then reuses images.sellable_file_bytes VERBATIM -- the same
deterministic max-quality sRGB re-encode M5a already ships for the digital
path -- with pod.print_file.max_bytes as the size cap instead of listing.json's
sellable_max_bytes. No resize, no AI, no generative anything: the photograph
is untouched (decision 34).

Hosting is a thin pass-through to whichever PrintFileHost the caller
constructed (Fake by default, Live only once pipeline.live_gate.
live_printfile_open() has confirmed the gate, design §9) -- build.py events
only `host`/`file_key`/`expires_at`/`sha256` from the returned HostedFile,
NEVER the signed url itself (design §3, decision 48: an append-only log must
not hold a credential).

Source-asset head (design 2026-08-11-source-asset-head, slice 2):
resolve_print_source_path falls back to the managed archive
(proj_asset_store) when the landing row is gone or its file has been
deleted from disk -- the on-disk landing path stays the FAST path (checked
first); the archive is the durable fallback, sha256-verified on read so a
corrupt/truncated archived master fails loudly instead of shipping silently.
"""

import hashlib
import os
import sqlite3

from shopsteward.adapters.printfile.interface import HostedFile, PrintFileHost
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings.images import sellable_file_bytes
from shopsteward.pipeline.listings.models import SellableFile

_PREFERRED_FORMAT = {"tiff_master": "TIFF", "jpeg": "JPEG"}


def resolve_print_source_row(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str, prefer: str
) -> sqlite3.Row:
    """The proj_landing_files row that supplies `landing_file_id`'s print
    master. If the row itself is already in the preferred format, use it
    as-is; otherwise look for a sibling row (same base_name, else same
    photo_id) in that format, falling back to the row's own path if no
    sibling exists (an operator who lands only a JPEG, no TIFF master,
    still gets a print file -- just not the preferred one)."""
    row = conn.execute(
        "SELECT path, base_name, photo_id, format, width, height FROM proj_landing_files "
        "WHERE user_id=? AND file_id=?",
        (user_id, landing_file_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"landing file {landing_file_id!r} not found for user {user_id}")

    wanted = _PREFERRED_FORMAT.get(prefer)
    if wanted is None or row["format"] == wanted:
        return row

    sibling = conn.execute(
        "SELECT path, base_name, photo_id, format, width, height FROM proj_landing_files "
        "WHERE user_id=? AND status='valid' "
        "AND format=? AND file_id != ? "
        "AND ((base_name IS NOT NULL AND base_name=?) OR (photo_id IS NOT NULL AND photo_id=?)) "
        "ORDER BY file_id LIMIT 1",
        (user_id, wanted, landing_file_id, row["base_name"], row["photo_id"]),
    ).fetchone()
    return sibling if sibling is not None else row


def _resolve_from_archive(
    conn: sqlite3.Connection, user_id: int, photo_id: str, fmt: str | None
) -> str:
    cfg = asset_store_config.get_asset_store_config(conn, user_id)
    archived = conn.execute(
        "SELECT stored_key, sha256 FROM proj_asset_store "
        "WHERE user_id=? AND photo_id=? AND format=?",
        (user_id, photo_id, fmt),
    ).fetchone()
    if archived is None:
        raise LookupError(
            f"landing file missing on disk and no archived master for photo {photo_id!r} "
            f"format {fmt!r} (user {user_id})"
        )

    path = asset_store_config.resolve_root(cfg) / archived["stored_key"]
    if not path.exists():
        raise FileNotFoundError(f"archived master missing on disk: {path}")

    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != archived["sha256"]:
        raise ValueError(f"archived master corrupt: sha256 mismatch for photo {photo_id!r}")
    return str(path)


def resolve_print_source_path(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str, prefer: str
) -> str:
    """The path to feed sellable_file_bytes for `landing_file_id`'s print
    file. Prefers the on-disk landing path (resolve_print_source_row); if
    that path no longer exists (the landing folder was cleared), falls back
    to the managed archive for the same photo_id/format."""
    row = resolve_print_source_row(conn, user_id, landing_file_id, prefer)
    if os.path.exists(row["path"]):
        return row["path"]
    if row["photo_id"] is None:
        raise FileNotFoundError(
            f"print master missing on disk and no photo_id to archive-fallback: {row['path']!r}"
        )
    return _resolve_from_archive(conn, user_id, row["photo_id"], row["format"])


def prepare_print_file(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str, prefer: str, max_bytes: int
) -> tuple[bytes, SellableFile]:
    """Resolve + read the print-master bytes for `landing_file_id`. Returns
    the same (bytes, SellableFile) shape push.py's sellable_file_bytes call
    already returns for the digital path -- deterministic, side-effect-free,
    recomputable on demand (never persists a derived JPEG to disk)."""
    path = resolve_print_source_path(conn, user_id, landing_file_id, prefer)
    return sellable_file_bytes(path, max_bytes)


def publish_print_file(
    host: PrintFileHost, data: bytes, sellable: SellableFile, *, ttl_seconds: int
) -> HostedFile:
    """Publish `data` through `host`, naming the object after its own
    sha256 (stable across retries, unlike a rotating signed url)."""
    return host.publish(data, name=sellable.sha256, ttl_seconds=ttl_seconds)
