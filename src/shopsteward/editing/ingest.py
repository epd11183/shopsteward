"""Folder-pointed ingestion: RAW+JPEG pairing, content-addressed identity,
EXIF capture, and event emission."""

import hashlib
import sqlite3
import uuid
from pathlib import Path

from PIL import ExifTags, Image

from shopsteward.core.events import Event, append
from shopsteward.editing.models import IngestReport

RAW_SUFFIXES = {".cr3"}
JPEG_SUFFIXES = {".jpg", ".jpeg"}
_CHUNK_SIZE = 64 * 1024

# Reverse lookup: Pillow's ExifTags.TAGS maps numeric tag id -> name; we want name -> id.
_EXIF_TAG_IDS = {name: tag_id for tag_id, name in ExifTags.TAGS.items()}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_exif(jpeg_path: Path) -> dict:
    try:
        with Image.open(jpeg_path) as img:
            exif = img.getexif()
            width, height = img.size
            fields = {"width": width, "height": height}
            wanted = {
                "DateTimeOriginal": 36867,
                "DateTime": 306,
                "Model": 272,
                "LensModel": 42036,
                "ISOSpeedRatings": 34855,
            }
            for name, tag_id in wanted.items():
                value = exif.get(tag_id)
                if value is not None:
                    fields[name] = value
            return fields
    except Exception:
        return {}


def _known_raw_hashes(conn: sqlite3.Connection, user_id: int) -> dict[str, str]:
    from shopsteward.core.events import read_all

    hashes: dict[str, str] = {}
    for e in read_all(conn, "photo.ingested"):
        if e.user_id == user_id:
            hashes[e.payload["raw_sha256"]] = e.payload["photo_id"]
    return hashes


def ingest_folder(
    conn: sqlite3.Connection,
    user_id: int,
    path: Path,
    mode: str,
    preset_family: str | None = None,
    event_name: str | None = None,
    output_folder: str | None = None,
) -> IngestReport:
    ingest_job_id = str(uuid.uuid4())
    append(
        conn,
        Event(
            user_id=user_id,
            type="ingest.started",
            payload={
                "ingest_job_id": ingest_job_id,
                "path": str(path),
                "mode": mode,
                "preset_family": preset_family,
                "event_name": event_name,
                "output_folder": output_folder,
            },
        ),
    )

    files = list(Path(path).iterdir()) if Path(path).is_dir() else []
    raws: dict[str, Path] = {}
    jpegs: dict[str, Path] = {}
    for f in files:
        if not f.is_file():
            continue
        suffix = f.suffix.lower()
        stem_key = f.stem.lower()
        if suffix in RAW_SUFFIXES:
            raws[stem_key] = f
        elif suffix in JPEG_SUFFIXES:
            jpegs[stem_key] = f

    known_hashes = _known_raw_hashes(conn, user_id)
    status = "awaiting_scoring" if mode == "hero" else "queued_for_edit"

    paired = 0
    duplicates = 0
    unpaired = 0
    photo_ids: list[str] = []

    all_stems = set(raws) | set(jpegs)
    for stem_key in sorted(all_stems):
        raw_path = raws.get(stem_key)
        jpeg_path = jpegs.get(stem_key)

        if raw_path is None:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="photo.unpaired",
                    payload={
                        "ingest_job_id": ingest_job_id,
                        "path": str(jpeg_path),
                        "reason": "missing_raw",
                    },
                ),
            )
            unpaired += 1
            continue
        if jpeg_path is None:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="photo.unpaired",
                    payload={
                        "ingest_job_id": ingest_job_id,
                        "path": str(raw_path),
                        "reason": "missing_jpeg",
                    },
                ),
            )
            unpaired += 1
            continue

        raw_sha256 = _sha256_file(raw_path)
        existing_photo_id = known_hashes.get(raw_sha256)
        if existing_photo_id is not None:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="photo.duplicate_skipped",
                    payload={
                        "ingest_job_id": ingest_job_id,
                        "raw_sha256": raw_sha256,
                        "raw_path": str(raw_path),
                        "existing_photo_id": existing_photo_id,
                    },
                ),
            )
            duplicates += 1
            continue

        photo_id = raw_sha256
        exif = _extract_exif(jpeg_path)
        append(
            conn,
            Event(
                user_id=user_id,
                type="photo.ingested",
                payload={
                    "photo_id": photo_id,
                    "ingest_job_id": ingest_job_id,
                    "base_name": raw_path.stem,
                    "raw_path": str(raw_path),
                    "jpeg_path": str(jpeg_path),
                    "raw_sha256": raw_sha256,
                    "exif": exif,
                    "mode": mode,
                    "status": status,
                },
            ),
        )
        known_hashes[raw_sha256] = photo_id
        photo_ids.append(photo_id)
        paired += 1

    append(
        conn,
        Event(
            user_id=user_id,
            type="ingest.completed",
            payload={
                "ingest_job_id": ingest_job_id,
                "paired": paired,
                "duplicates": duplicates,
                "unpaired": unpaired,
            },
        ),
    )

    return IngestReport(
        ingest_job_id=ingest_job_id,
        mode=mode,
        paired=paired,
        duplicates=duplicates,
        unpaired=unpaired,
        photo_ids=photo_ids,
    )
