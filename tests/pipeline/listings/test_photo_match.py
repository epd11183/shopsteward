import io

from PIL import Image, ImageDraw

from shopsteward.pipeline.listings.photo_match import hamming_distance, phash_bytes


def _synthetic_jpeg(*, quality: int = 95, size: int = 256) -> bytes:
    img = Image.new("RGB", (size, size), (20, 40, 80))
    d = ImageDraw.Draw(img)
    d.ellipse((size // 4, size // 4, size * 3 // 4, size * 3 // 4), fill=(230, 160, 40))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _unrelated_jpeg(size: int = 256) -> bytes:
    img = Image.new("RGB", (size, size), (10, 10, 10))
    d = ImageDraw.Draw(img)
    for x in range(0, size, 16):
        d.line((x, 0, x, size), fill=(255, 255, 255), width=2)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def test_identical_bytes_hash_to_zero_distance():
    original = _synthetic_jpeg()
    h = phash_bytes(original)
    assert hamming_distance(h, phash_bytes(original)) == 0


def test_dc_bit_is_genuinely_excluded_not_a_coincidental_one():
    # The DC coefficient (index 0, assembled as the hash's MSB) dominates
    # magnitude and would almost always threshold True against the AC
    # median -- it must be a fixed, documented 0, not an accident of the
    # threshold math. Assert it across varied real images, not just one.
    images = [_synthetic_jpeg(quality=q) for q in (30, 55, 75, 95)] + [_unrelated_jpeg()]
    for img_bytes in images:
        h = phash_bytes(img_bytes)
        assert h < (1 << 63), "DC bit (hash MSB) must be 0 -- genuinely excluded"


def test_recompressed_copy_matches_within_threshold_unrelated_does_not():
    original = _synthetic_jpeg(quality=95)
    recompressed = _synthetic_jpeg(quality=55)  # re-encoded, same content
    unrelated = _unrelated_jpeg()

    h_original = phash_bytes(original)
    h_recompressed = phash_bytes(recompressed)
    h_unrelated = phash_bytes(unrelated)

    dist_same = hamming_distance(h_original, h_recompressed)
    dist_diff = hamming_distance(h_original, h_unrelated)

    assert dist_same <= 6  # default max_distance
    assert dist_diff > 6
    assert dist_same < dist_diff
