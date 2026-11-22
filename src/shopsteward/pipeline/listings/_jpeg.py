"""Shared JPEG quality-ladder encoder (images.py + digital_files.py)."""

import io
from typing import Any

from PIL import Image

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
