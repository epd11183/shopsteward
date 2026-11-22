"""PURE image-order + sellable-file resolution: no DB/event I/O. File I/O
(reading the landing original, re-encoding a derived JPEG) is deterministic
and side-effect-free -- same inputs always produce the same bytes/hash, so
callers never need to persist a derived JPEG; a later push stage can
recompute it on demand."""

import hashlib
import io
from pathlib import Path
from typing import Any

from PIL import Image

from shopsteward.pipeline.listings.models import ListingConfig, ListingImage, SellableFile

_TIFF_SUFFIXES = {".tif", ".tiff"}

# Descending JPEG quality ladder for the oversized/TIFF fallback. Starts at
# 100 so a TIFF (or any file that only needed re-encoding, not shrinking)
# still gets a genuinely max-quality JPEG -- only files that don't fit at
# 100 pay for a second encode. Finer steps near the top (95/90/85) since
# that's where a print product's quality is most sensitive; coarser below
# since q100->q60 already recovers the vast majority of a full-res photo's
# size.
_QUALITY_STEPS = (100, 95, 90, 85, 80, 75, 70, 65, 60, 55, 50)


class SellableFileTooLargeError(OSError):
    """Raised when even the lowest quality step in _QUALITY_STEPS still
    exceeds sellable_max_bytes. OSError subclass so it's caught by the
    existing `except (EtsyWriteError, OSError)` handling in push.py/gate3.py
    without changes there -- one broken file fails that draft, not the
    batch. ponytail: the ceiling is quality 50 with no resize; only a
    genuinely huge image resolution (well beyond typical camera output)
    should ever hit it -- if it does in practice, add a resize step."""


def encode_jpeg_under_cap(
    img: Image.Image,
    max_bytes: int,
    *,
    label: str = "image",
    cap_name: str = "sellable_max_bytes",
    **save_kw: Any,
) -> bytes:
    """Encode ``img`` as JPEG, stepping quality down _QUALITY_STEPS until the
    result is <= max_bytes. Extra ``save_kw`` (icc_profile, dpi, exif, ...) pass
    through to Pillow. Raises SellableFileTooLargeError if even the lowest
    step doesn't fit. Shared by images.py and digital_files.py."""
    last = b""
    for quality in _QUALITY_STEPS:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, **save_kw)
        last = buf.getvalue()
        if len(last) <= max_bytes:
            return last
    raise SellableFileTooLargeError(
        f"{label}: even quality={_QUALITY_STEPS[-1]} JPEG ({len(last)} bytes) exceeds "
        f"{cap_name}={max_bytes}; resolution is too high to fit without a resize"
    )


def order_listing_images(mockups: list[dict], config: ListingConfig) -> list[ListingImage]:
    """Orders mockup rows ({"path", "intent"}) per config.image_order (hero
    ``single`` first, ``digital_whatyougot`` included), capped at
    config.image_cap. Intents absent from image_order sort last. Ties within
    an intent keep the caller's input order (callers pass rows pre-sorted by
    path for determinism)."""
    order_index = {intent: i for i, intent in enumerate(config.image_order)}
    ranked = sorted(
        enumerate(mockups),
        key=lambda pair: (order_index.get(pair[1]["intent"], len(order_index)), pair[0]),
    )
    capped = ranked[: config.image_cap]
    return [
        ListingImage(path=row["path"], intent=row["intent"], rank=rank)
        for rank, (_, row) in enumerate(capped, start=1)
    ]


def _load_sellable(path: str, sellable_max_bytes: int) -> tuple[bytes, SellableFile]:
    raw = Path(path).read_bytes()
    is_tiff = Path(path).suffix.lower() in _TIFF_SUFFIXES

    if not is_tiff and len(raw) <= sellable_max_bytes:
        return raw, SellableFile(
            source="landing_original", sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)
        )

    with Image.open(path) as img:
        derived = encode_jpeg_under_cap(img.convert("RGB"), sellable_max_bytes, label=path)
    return derived, SellableFile(
        source="derived_jpeg", sha256=hashlib.sha256(derived).hexdigest(), bytes=len(derived)
    )


def resolve_sellable_file(path: str, sellable_max_bytes: int) -> SellableFile:
    """Sellable file = the landing original, unless it's a TIFF or exceeds
    sellable_max_bytes -- then a deterministic sRGB JPEG (Pillow re-encode,
    no resize, no AI) is produced instead, stepping quality down through
    _QUALITY_STEPS (starting at 100) until it fits. Raises
    SellableFileTooLargeError if it still doesn't fit at the lowest step."""
    _, meta = _load_sellable(path, sellable_max_bytes)
    return meta


def sellable_file_bytes(path: str, sellable_max_bytes: int) -> tuple[bytes, SellableFile]:
    """Same resolution as resolve_sellable_file, but also returns the bytes
    to upload -- used by the push stage, which needs the actual file, not
    just its metadata. Deterministic and side-effect-free, so the push stage
    can recompute it on demand (never persists a derived JPEG)."""
    return _load_sellable(path, sellable_max_bytes)
