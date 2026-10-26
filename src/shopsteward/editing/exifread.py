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
            # Camera EXIF is split across IFD0 (Model, DateTime) and the
            # nested Exif sub-IFD at 0x8769 (DateTimeOriginal, LensModel,
            # ISOSpeedRatings). get_ifd() returns {} rather than raising
            # when a JPEG has no sub-IFD, so a plain merge is safe.
            merged = dict(exif) | dict(exif.get_ifd(0x8769))
            return {"width": width, "height": height} | _pick(merged)
    except Exception:
        return {}


def _pick(merged: dict) -> dict:
    fields: dict = {}
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


def read_cr3_exif(path: Path) -> dict:
    """EXIF straight from a Canon CR3 when no sibling JPEG exists. CR3 is
    ISO-BMFF: the CMT1 box holds IFD0 (Model, DateTime) and CMT2 the Exif
    sub-IFD (ISO, DateTimeOriginal, LensModel), each as a bare TIFF blob.
    Non-CR3 or unparseable files return {} (same contract as read_jpeg_exif)."""
    try:
        if Path(path).suffix.lower() != ".cr3":
            return {}
        with open(path, "rb") as f:
            head = f.read(1 << 20)  # ponytail: CMT boxes sit in the moov header, well inside 1 MiB
        merged: dict = {}
        for box in (b"CMT1", b"CMT2"):
            i = head.find(box)
            if i < 4:
                continue
            size = int.from_bytes(head[i - 4 : i], "big")
            exif = Image.Exif()
            exif.load(head[i + 4 : i - 4 + size])
            merged |= dict(exif)
        return _pick(merged)
    except Exception:
        return {}
