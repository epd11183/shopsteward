"""PURE image-order + sellable-file resolution: no DB/event I/O. File I/O
(reading the landing original, re-encoding a derived JPEG) is deterministic
and side-effect-free -- same inputs always produce the same bytes/hash, so
callers never need to persist a derived JPEG; a later push stage can
recompute it on demand."""

import hashlib
import io
from pathlib import Path

from PIL import Image

from shopsteward.pipeline.listings.models import ListingConfig, ListingImage, SellableFile

_TIFF_SUFFIXES = {".tif", ".tiff"}


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


def resolve_sellable_file(path: str, sellable_max_bytes: int) -> SellableFile:
    """Sellable file = the landing original, unless it's a TIFF or exceeds
    sellable_max_bytes -- then a deterministic max-quality sRGB JPEG
    (Pillow re-encode, no resize, no AI) is produced instead."""
    raw = Path(path).read_bytes()
    is_tiff = Path(path).suffix.lower() in _TIFF_SUFFIXES

    if not is_tiff and len(raw) <= sellable_max_bytes:
        return SellableFile(
            source="landing_original", sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)
        )

    with Image.open(path) as img:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=100)
        derived = buf.getvalue()
    return SellableFile(
        source="derived_jpeg", sha256=hashlib.sha256(derived).hexdigest(), bytes=len(derived)
    )
