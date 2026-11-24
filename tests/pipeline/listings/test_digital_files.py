import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from shopsteward.pipeline.listings.digital_files import (
    _SRGB_ICC,
    DigitalFilesConfig,
    _is_srgb,
    encode_file,
    photo_dims,
    plan_files,
)
from shopsteward.pipeline.listings.images import SellableFileTooLargeError

_DEFAULTS = Path(__file__).resolve().parents[3] / "config" / "defaults" / "listing.json"


@pytest.fixture
def cfg() -> DigitalFilesConfig:
    raw = json.loads(_DEFAULTS.read_text("utf-8"))["digital_files"]
    return DigitalFilesConfig.model_validate(raw)


def _by_key(plan):
    return {f.key: f for f in plan.files}


def test_24mp_landscape_table(cfg):
    plan = plan_files((6000, 4000), None, cfg)
    f = _by_key(plan)
    assert [x.rank for x in plan.files] == [1, 2, 3, 4, 5]
    assert f["full"].sizes == ["4x6", "8x12", "12x18"]
    assert f["4x5"].sizes == ["4x5", "8x10"]
    assert f["4x5"].crop_box == (500, 0, 5500, 4000)
    assert f["5x7"].sizes == ["5x7"]
    assert f["11x14"].sizes == ["11x14"]
    assert f["A"].sizes == ["A4", "A3"]


def test_45mp_landscape_table(cfg):
    plan = plan_files((8256, 5504), None, cfg)
    f = _by_key(plan)
    assert [x.rank for x in plan.files] == [1, 2, 3, 4, 5]
    assert f["A"].crop_box == (236, 0, 8020, 5504)
    assert f["full"].sizes == ["4x6", "8x12", "12x18", "16x24"]
    assert f["4x5"].sizes == ["4x5", "8x10", "16x20"]
    assert f["5x7"].sizes == ["5x7"]
    assert f["11x14"].sizes == ["11x14"]
    assert f["A"].sizes == ["A4", "A3", "A2"]


def test_portrait_crops_are_portrait(cfg):
    f = _by_key(plan_files((4000, 6000), None, cfg))
    left, top, right, bottom = f["4x5"].crop_box
    assert (right - left, bottom - top) == (4000, 5000)
    assert f["4x5"].sizes == ["4x5", "8x10"]


def test_square_treated_as_portrait_and_full_has_no_sizes(cfg):
    f = _by_key(plan_files((4000, 4000), None, cfg))
    assert f["full"].sizes == []
    left, top, right, bottom = f["4x5"].crop_box
    assert (right - left, bottom - top) == (3200, 4000)


def test_panorama_drops_ratio_crops(cfg):
    plan = plan_files((6000, 2000), None, cfg)
    assert [x.key for x in plan.files] == ["full"]
    assert plan.files[0].sizes == []


def test_ratio_with_zero_sizes_dropped_but_full_kept(cfg):
    plan = plan_files((600, 400), None, cfg)
    assert [x.key for x in plan.files] == ["full"]
    assert plan.files[0].sizes == []


def test_focus_stored_clamped(cfg):
    assert plan_files((6000, 4000), (2.0, -1.0), cfg).focus == (1.0, 0.0)


def test_focus_clamped_inside_frame(cfg):
    for focus, left in (((0.0, 0.5), 0), ((1.0, 0.5), 1000), ((2.0, -1.0), 1000)):
        box = _by_key(plan_files((6000, 4000), focus, cfg))["4x5"].crop_box
        assert box[0] == left and box[2] - box[0] == 5000
        assert box[1] >= 0 and box[3] <= 4000


def test_max_files_caps(cfg):
    small = cfg.model_copy(update={"max_files": 2})
    assert [x.rank for x in plan_files((6000, 4000), None, small).files] == [1, 2]


def test_photo_dims_applies_exif_orientation(tmp_path):
    p = tmp_path / "rot.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (60, 40)).save(p, exif=exif.tobytes())
    assert photo_dims(p) == (40, 60)


def _source(path: Path) -> None:
    exif = Image.Exif()
    exif[0x9C9C] = "junk"  # must not survive
    exif.get_ifd(0x8769)[0x9003] = "2024:05:01 10:00:00"
    exif.get_ifd(0x8825)[1] = "N"  # GPS IFD
    Image.new("RGB", (600, 400), (200, 100, 50)).save(
        path, exif=exif.tobytes(), comment=b"SERIAL 12345"
    )


def _reopen(tmp_path, data):
    out = tmp_path / "o.jpg"
    out.write_bytes(data)
    return Image.open(out)


def test_encode_file(tmp_path, cfg):
    src = tmp_path / "s.jpg"
    _source(src)
    tcfg = cfg.model_copy(update={"ppi": 30, "copyright_holder": "Test Holder"})
    entry = _by_key(plan_files(photo_dims(src), None, tcfg))["4x5"]
    data, sha = encode_file(src, entry, tcfg)
    with _reopen(tmp_path, data) as im:
        assert im.size == (500, 400)
        assert round(im.info["dpi"][0]) == 30
        assert im.info["icc_profile"] == _SRGB_ICC
        assert _is_srgb(ImageCms.ImageCmsProfile(io.BytesIO(im.info["icc_profile"])))
        ex = im.getexif()
        assert ex[0x013B] == "Test Holder"
        assert ex[0x8298] == "(c) 2024 Test Holder"
        assert not ex.get_ifd(0x8825)
        assert 0x0112 not in ex
        assert 0x9C9C not in ex
        assert "comment" not in im.info
    assert encode_file(src, entry, tcfg) == (data, sha)


def test_encode_without_holder_has_no_exif(tmp_path, cfg):
    src = tmp_path / "s.jpg"
    _source(src)
    tcfg = cfg.model_copy(update={"ppi": 30, "copyright_holder": None})
    entry = _by_key(plan_files(photo_dims(src), None, tcfg))["full"]
    data, _ = encode_file(src, entry, tcfg)
    with _reopen(tmp_path, data) as im:
        assert not im.getexif()
        assert im.size == (600, 400)


def test_encode_too_large_raises(tmp_path, cfg):
    src = tmp_path / "s.jpg"
    _source(src)
    tcfg = cfg.model_copy(update={"ppi": 30, "max_bytes": 10})
    entry = _by_key(plan_files((600, 400), None, tcfg))["full"]
    with pytest.raises(SellableFileTooLargeError):
        encode_file(src, entry, tcfg)


def test_srgb_icc_is_stable():
    assert _SRGB_ICC[24:36] == bytes(12)
    assert _SRGB_ICC[84:100] == bytes(16)


# libjpeg builds differ between the Windows and Linux Pillow wheels, so the byte pin
# only holds on the dev box; same-platform determinism is test_encode_file's job.
@pytest.mark.skipif(sys.platform != "win32", reason="golden bytes pinned to the Windows wheel")
def test_encode_golden_sha(tmp_path, cfg):
    # Pinned: a Pillow/lcms upgrade that changes this must be a DELIBERATE update.
    src = tmp_path / "s.jpg"
    _source(src)
    tcfg = cfg.model_copy(update={"ppi": 30, "copyright_holder": "Test Holder"})
    entry = _by_key(plan_files(photo_dims(src), None, tcfg))["4x5"]
    assert (
        encode_file(src, entry, tcfg)[1]
        == "4d245579fee9a4b70f737819d3805d15f9230f3674ed0f9ec3f3f7aae663d201"
    )


def _swapped_primaries_profile() -> bytes:
    """A genuinely different RGB profile: sRGB with the red and green primary
    tags (rXYZ/gXYZ) swapped and the description renamed."""
    raw = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    count = int.from_bytes(raw[128:132], "big")
    tags = {}
    for i in range(count):
        o = 132 + 12 * i
        tags[bytes(raw[o : o + 4])] = (
            int.from_bytes(raw[o + 4 : o + 8], "big"),
            int.from_bytes(raw[o + 8 : o + 12], "big"),
        )
    (ro, rs), (go, gs) = tags[b"rXYZ"], tags[b"gXYZ"]
    assert rs == gs
    r, g = bytes(raw[ro : ro + rs]), bytes(raw[go : go + gs])
    raw[ro : ro + rs], raw[go : go + gs] = g, r
    return (
        bytes(raw)
        .replace(b"sRGB", b"xRGB")
        .replace("sRGB".encode("utf-16-be"), "xRGB".encode("utf-16-be"))
    )


def test_non_srgb_source_converted_to_srgb(tmp_path, cfg):
    src = tmp_path / "s.jpg"
    other = _swapped_primaries_profile()
    assert not _is_srgb(ImageCms.ImageCmsProfile(io.BytesIO(other)))
    Image.new("RGB", (60, 40), (10, 200, 30)).save(src, icc_profile=other)
    tcfg = cfg.model_copy(update={"ppi": 30})
    entry = _by_key(plan_files((60, 40), None, tcfg))["full"]
    data, _ = encode_file(src, entry, tcfg)
    with _reopen(tmp_path, data) as im:
        assert im.info["icc_profile"] == _SRGB_ICC
        r, g, b = im.getpixel((30, 20))
    # Pixels really changed (the profile was applied), not just relabelled.
    assert (r, g, b) != (10, 200, 30)
    assert r > 100 > g


def test_corrupt_icc_raises_value_error(tmp_path, cfg):
    src = tmp_path / "bad.jpg"
    Image.new("RGB", (60, 40)).save(src, icc_profile=b"not an icc profile")
    tcfg = cfg.model_copy(update={"ppi": 30})
    entry = _by_key(plan_files((60, 40), None, tcfg))["full"]
    with pytest.raises(ValueError, match="bad.jpg"):
        encode_file(src, entry, tcfg)


def test_icc_transform_failure_raises_value_error(tmp_path, cfg, monkeypatch):
    src = tmp_path / "t.jpg"
    Image.new("RGB", (60, 40)).save(src, icc_profile=_swapped_primaries_profile())

    def boom(*a, **k):
        raise ImageCms.PyCMSError("cannot build transform")

    monkeypatch.setattr(ImageCms, "profileToProfile", boom)
    tcfg = cfg.model_copy(update={"ppi": 30})
    entry = _by_key(plan_files((60, 40), None, tcfg))["full"]
    with pytest.raises(ValueError, match="t.jpg"):
        encode_file(src, entry, tcfg)
