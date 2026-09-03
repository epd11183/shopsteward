"""On-demand landing_dir() scan: technical validation only; base-name match to
proj_photos; landing.* events. Stops there -- listing creation is M4."""

import hashlib
import io
import sqlite3
from pathlib import Path

from PIL import Image, ImageCms

from shopsteward.core.events import Event, append, read_all
from shopsteward.editing.projections import rebuild_editing
from shopsteward.pipeline import tuning
from shopsteward.pipeline.models import LandingReport
from shopsteward.pipeline.projections import rebuild_pipeline
from shopsteward.settings import landing_dir

_SUFFIX_FORMATS = {".tif": "TIFF", ".tiff": "TIFF", ".jpg": "JPEG", ".jpeg": "JPEG"}
_CHUNK_SIZE = 64 * 1024
_DETECTABLE_MODES = ("RGB", "RGBA", "L")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _known_file_ids(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """File ids already recorded — valid or invalid — so an unfixed bad file
    doesn't re-emit landing.file_invalid on every scan. A fixed file has new
    bytes, hence a new sha256, and is picked up normally.

    An ordered fold, not a set comprehension: a landing.file_reset event (id
    order, read_all is ORDER BY id) removes a file_id from this set so
    scan_landing re-observes it -- the content hash is unchanged so it gets
    the SAME file_id back (winners-batch reset, pipeline/listings/reset.py)."""
    known: set[str] = set()
    for e in read_all(conn, "landing.file_"):
        if e.user_id != user_id or not e.payload.get("file_id"):
            continue
        if e.type == "landing.file_reset":
            known.discard(e.payload["file_id"])
        else:
            known.add(e.payload["file_id"])
    return known


def folder_file_ids(folder: Path) -> dict[Path, str]:
    """sha256 every recognized landing-format file directly in `folder`
    (non-recursive, mirrors scan_landing's own top-level iterdir) -- used by
    the winners-batch reset (pipeline/listings/reset.py) to find which
    proj_landing_files rows correspond to files still present in a named
    folder, without duplicating scan_landing's own hashing logic."""
    result: dict[Path, str] = {}
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in _SUFFIX_FORMATS:
            continue
        result[f] = _sha256_file(f)
    return result


def _known_base_names(conn: sqlite3.Connection, user_id: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT base_name, photo_id FROM proj_photos WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    pairs = [(row["base_name"], row["photo_id"]) for row in rows]
    # Longest base_name first so the most specific match wins.
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def _match_photo(stem: str, base_names: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    for base_name, photo_id in base_names:
        if stem.startswith(base_name):
            return base_name, photo_id
    return None, None


def _color_space(mode: str, info: dict) -> str:
    icc = info.get("icc_profile")
    if icc:
        try:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            description = ImageCms.getProfileDescription(profile)
        except Exception:
            return mode
        return description.strip() if description else mode
    if mode in _DETECTABLE_MODES:
        return mode
    return "unknown"


def _validate(path: Path, *, fmt: str, allowed_formats: list[str], min_long_edge_px: int) -> dict:
    """Returns {"reason": str} if invalid, else {"width", "height", "color_space"}."""
    if fmt not in allowed_formats:
        return {"reason": "unsupported_format"}

    try:
        with Image.open(path) as img:
            img.verify()  # invalidates the handle -- must reopen to read size/mode
    except Exception:
        return {"reason": "unreadable"}

    try:
        with Image.open(path) as img:
            width, height = img.size
            mode = img.mode
            info = img.info
    except Exception:
        return {"reason": "unreadable"}

    if max(width, height) < min_long_edge_px:
        return {"reason": "below_min_resolution"}

    color_space = _color_space(mode, info)
    if color_space == "unknown":
        return {"reason": "unknown_color_space"}

    return {"width": width, "height": height, "color_space": color_space}


def scan_landing(
    conn: sqlite3.Connection, user_id: int, landing_path: Path | None = None
) -> LandingReport:
    path = Path(landing_path) if landing_path is not None else landing_dir()
    if not path.is_dir():
        return LandingReport()

    rebuild_editing(conn)  # ensure proj_photos is fresh for base_name matching
    profile = tuning.get_profile(conn, user_id)
    landing_cfg = profile.landing

    known_file_ids = _known_file_ids(conn, user_id)
    base_names = _known_base_names(conn, user_id)

    observed = matched = invalid = 0

    for f in sorted(path.iterdir()):
        if not f.is_file():
            continue
        fmt = _SUFFIX_FORMATS.get(f.suffix.lower())
        if fmt is None:
            continue

        file_id = _sha256_file(f)
        if file_id in known_file_ids:
            continue

        result = _validate(
            f,
            fmt=fmt,
            allowed_formats=landing_cfg.allowed_formats,
            min_long_edge_px=landing_cfg.min_long_edge_px,
        )
        if "reason" in result:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="landing.file_invalid",
                    payload={"file_id": file_id, "path": str(f), "reason": result["reason"]},
                ),
            )
            known_file_ids.add(file_id)
            invalid += 1
            continue

        base_name, photo_id = _match_photo(f.stem, base_names)
        append(
            conn,
            Event(
                user_id=user_id,
                type="landing.file_observed",
                payload={
                    "file_id": file_id,
                    "path": str(f),
                    "base_name": base_name,
                    "format": fmt,
                    "width": result["width"],
                    "height": result["height"],
                    "color_space": result["color_space"],
                    "photo_id": photo_id,
                },
            ),
        )
        known_file_ids.add(file_id)
        observed += 1
        if photo_id is not None:
            matched += 1

    rebuild_pipeline(conn)
    return LandingReport(
        observed=observed, matched=matched, manual_drops=observed - matched, invalid=invalid
    )


def reset_file(conn: sqlite3.Connection, user_id: int, file_id: str, *, reason: str) -> None:
    """Appends landing.file_reset for one file_id (winners-batch reset,
    pipeline/listings/reset.py). Purely additive -- the original
    landing.file_observed event is never touched; only the derived
    proj_landing_files row is removed on rebuild (pipeline/projections.py),
    and _known_file_ids() forgets the id so a follow-up scan_landing()
    re-observes it (refreshing path/width/height if the winners folder
    moved) with the SAME file_id, since the bytes are unchanged."""
    row = conn.execute(
        "SELECT path FROM proj_landing_files WHERE user_id=? AND file_id=?", (user_id, file_id)
    ).fetchone()
    append(
        conn,
        Event(
            user_id=user_id,
            type="landing.file_reset",
            payload={
                "file_id": file_id,
                "reason": reason,
                "prior_path": row["path"] if row is not None else None,
            },
        ),
    )
