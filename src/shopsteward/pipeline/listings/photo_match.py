"""Perceptual hash (pHash) for matching a local archive photo against an
Etsy listing's product image (design: source-photo-match backfill). Uses
OpenCV -- already a project dependency (opencv-python-headless, pyproject.toml)
-- and numpy, both already used the same way by mockups/compositor.py. No
new dependency.

Algorithm: resize to 32x32 grayscale, 2D DCT, take the top-left 8x8
coefficient block, median-threshold every coefficient EXCEPT the DC term
(index 0 -- it dominates magnitude and would bias the threshold toward
"mostly 1s") against the median of the other 63. The DC bit itself is fixed
to 0 -- genuinely excluded, not merely excluded from the threshold
computation -- so the result is a 64-bit int with 63 meaningfully-computed
bits and one constant filler bit (same convention the imagehash library
uses). Reimplemented here in ~15 lines rather than adding a dependency for
it (CLAUDE.md: no new dependency without operator approval).

Pure functions, no I/O beyond decoding already-read bytes -- callers own
file/network reads."""

import cv2
import numpy as np

_RESIZE = 32
_HASH_SIZE = 8


def phash_bytes(image_bytes: bytes) -> int:
    """Decode arbitrary image bytes (JPEG/PNG/etc, whatever cv2 supports)
    and return a 64-bit perceptual hash as a plain int."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("phash_bytes: could not decode image bytes")
    return phash_array(img)


def phash_array(img: np.ndarray) -> int:
    resized = cv2.resize(img, (_RESIZE, _RESIZE), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(resized))
    block = dct[:_HASH_SIZE, :_HASH_SIZE].flatten()

    median = float(np.median(block[1:]))  # exclude the DC term (index 0)
    bits = block >= median
    bits[0] = False  # DC dominates magnitude and would otherwise almost always
    # threshold True against the AC median -- fix it to a constant so it is
    # genuinely excluded from the hash, not just from the threshold math.

    result = 0
    for bit in bits:
        result = (result << 1) | int(bit)
    return result


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


if __name__ == "__main__":
    # ponytail: smallest runnable self-check, not a test suite. Needs actual
    # texture (a flat color has no distinguishing DCT coefficients -- every
    # AC value ties the median -- so this draws a shape, same as the real
    # tests in tests/pipeline/listings/test_photo_match.py).
    import io

    from PIL import Image, ImageDraw

    def _jpeg_bytes(quality: int = 90, size: int = 128) -> bytes:
        img = Image.new("RGB", (size, size), (20, 40, 80))
        ImageDraw.Draw(img).ellipse(
            (size // 4, size // 4, size * 3 // 4, size * 3 // 4), fill=(230, 160, 40)
        )
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return buf.getvalue()

    def _stripes_jpeg(size: int = 128) -> bytes:
        img = Image.new("RGB", (size, size), (10, 10, 10))
        draw = ImageDraw.Draw(img)
        for x in range(0, size, 16):
            draw.line((x, 0, x, size), fill=(255, 255, 255), width=2)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=95)
        return buf.getvalue()

    original = _jpeg_bytes(quality=95)
    recompressed = _jpeg_bytes(quality=55)  # same content, re-encoded
    different = _stripes_jpeg()  # unrelated image

    h1 = phash_bytes(original)
    h2 = phash_bytes(recompressed)
    h3 = phash_bytes(different)

    assert hamming_distance(h1, h1) == 0
    assert hamming_distance(h1, h2) < hamming_distance(h1, h3)
    print("photo_match self-check OK")
