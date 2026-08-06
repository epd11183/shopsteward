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
"""

import sqlite3

from shopsteward.adapters.printfile.interface import HostedFile, PrintFileHost
from shopsteward.pipeline.listings.images import sellable_file_bytes
from shopsteward.pipeline.listings.models import SellableFile

_PREFERRED_FORMAT = {"tiff_master": "TIFF", "jpeg": "JPEG"}


def resolve_print_source_path(
    conn: sqlite3.Connection, user_id: int, landing_file_id: str, prefer: str
) -> str:
    """The path to feed sellable_file_bytes for `landing_file_id`'s print
    file. If the row itself is already in the preferred format, use it
    as-is; otherwise look for a sibling row (same base_name, else same
    photo_id) in that format, falling back to the row's own path if no
    sibling exists (an operator who lands only a JPEG, no TIFF master,
    still gets a print file -- just not the preferred one)."""
    row = conn.execute(
        "SELECT path, base_name, photo_id, format FROM proj_landing_files "
        "WHERE user_id=? AND file_id=?",
        (user_id, landing_file_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"landing file {landing_file_id!r} not found for user {user_id}")

    wanted = _PREFERRED_FORMAT.get(prefer)
    if wanted is None or row["format"] == wanted:
        return row["path"]

    sibling = conn.execute(
        "SELECT path FROM proj_landing_files WHERE user_id=? AND status='valid' "
        "AND format=? AND file_id != ? "
        "AND ((base_name IS NOT NULL AND base_name=?) OR (photo_id IS NOT NULL AND photo_id=?)) "
        "ORDER BY file_id LIMIT 1",
        (user_id, wanted, landing_file_id, row["base_name"], row["photo_id"]),
    ).fetchone()
    return sibling["path"] if sibling is not None else row["path"]


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
