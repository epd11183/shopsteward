"""Shared EXIF plumbing: sibling-JPEG resolution and EXIF extraction via Pillow.
Used by both ingest.py (event payload) and rawdecode.py (DecodedImage.exif) so
the pairing/read logic lives in exactly one place."""

from pathlib import Path

from PIL import Image

JPEG_SUFFIXES = {".jpg", ".jpeg"}

_WANTED_TAGS = {
    "DateTimeOriginal": 36867,
    "DateTime": 306,
    "Model": 272,
    "LensModel": 42036,
    "ISOSpeedRatings": 34855,
}


def sibling_jpeg(raw_path: Path) -> Path | None:
    """Resolve the JPEG paired with raw_path by stem, case-insensitively."""
    raw_path = Path(raw_path)
    stem_key = raw_path.stem.lower()
    parent = raw_path.parent
    if not parent.is_dir():
        return None
    for f in parent.iterdir():
        if f.is_file() and f.suffix.lower() in JPEG_SUFFIXES and f.stem.lower() == stem_key:
            return f
    return None


def read_jpeg_exif(path: Path) -> dict:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            width, height = img.size
            fields = {"width": width, "height": height}
            # Camera EXIF is split across IFD0 (Model, DateTime) and the
            # nested Exif sub-IFD at 0x8769 (DateTimeOriginal, LensModel,
            # ISOSpeedRatings). get_ifd() returns {} rather than raising
            # when a JPEG has no sub-IFD, so a plain merge is safe.
            merged = dict(exif) | dict(exif.get_ifd(0x8769))
            for name, tag_id in _WANTED_TAGS.items():
                value = merged.get(tag_id)
                if value is None:
                    continue
                if name == "ISOSpeedRatings":
                    # Some cameras report auto-ISO as a tuple/list; take the
                    # first value. IFDRational/int coerce directly via int().
                    if isinstance(value, (tuple, list)):
                        value = value[0] if value else None
                        if value is None:
                            continue
                    value = int(value)
                fields[name] = value
            return fields
    except Exception:
        return {}
