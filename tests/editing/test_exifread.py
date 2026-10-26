from pathlib import Path

from PIL import Image

from shopsteward.editing.exifread import read_cr3_exif, read_jpeg_exif, sibling_jpeg


def _make_jpeg(path: Path) -> None:
    Image.new("RGB", (8, 8)).save(path, "JPEG")


def test_sibling_jpeg_matches_case_insensitively(tmp_path: Path):
    raw = tmp_path / "IMG_1.CR3"
    raw.write_bytes(b"raw")
    _make_jpeg(tmp_path / "img_1.JPG")

    assert sibling_jpeg(raw) == tmp_path / "img_1.JPG"


def test_sibling_jpeg_returns_none_when_missing(tmp_path: Path):
    raw = tmp_path / "IMG_2.CR3"
    raw.write_bytes(b"raw")

    assert sibling_jpeg(raw) is None


def test_read_jpeg_exif_returns_dimensions(tmp_path: Path):
    jpeg = tmp_path / "a.jpg"
    _make_jpeg(jpeg)

    fields = read_jpeg_exif(jpeg)

    assert fields["width"] == 8 and fields["height"] == 8


def test_read_jpeg_exif_corrupt_file_returns_empty(tmp_path: Path):
    jpeg = tmp_path / "bad.jpg"
    jpeg.write_bytes(b"not a real jpeg")

    assert read_jpeg_exif(jpeg) == {}


def test_read_jpeg_exif_reads_tags_from_exif_sub_ifd(tmp_path: Path):
    """Real cameras split EXIF across IFD0 (Model, DateTime) and the nested
    Exif sub-IFD at tag 0x8769 (DateTimeOriginal, LensModel, ISOSpeedRatings).
    read_jpeg_exif must merge both, not just look at the top-level IFD."""
    jpeg = tmp_path / "camera.jpg"
    img = Image.new("RGB", (8, 8))
    exif = img.getexif()
    exif[272] = "Canon EOS-1D X"  # Model, IFD0
    exif[306] = "2026:08:20 14:35:53"  # DateTime, IFD0
    sub_ifd = exif.get_ifd(0x8769)
    sub_ifd[36867] = "2026:08:20 14:35:53"  # DateTimeOriginal
    sub_ifd[42036] = "RF24-70mm F2.8 L IS USM"  # LensModel
    sub_ifd[34855] = 10000  # ISOSpeedRatings
    img.save(jpeg, "JPEG", exif=exif)

    fields = read_jpeg_exif(jpeg)

    assert fields["Model"] == "Canon EOS-1D X"
    assert fields["DateTimeOriginal"] == "2026:08:20 14:35:53"
    assert fields["LensModel"] == "RF24-70mm F2.8 L IS USM"
    assert fields["ISOSpeedRatings"] == 10000
    assert isinstance(fields["ISOSpeedRatings"], int)


def _box(name: bytes, tags: dict) -> bytes:
    exif = Image.Exif()
    for k, v in tags.items():
        exif[k] = v
    blob = exif.tobytes()[6:]  # drop the "Exif\0\0" APP1 prefix; CR3 boxes hold bare TIFF
    return (len(blob) + 8).to_bytes(4, "big") + name + blob


def test_read_cr3_exif_reads_cmt_boxes(tmp_path: Path):
    raw = tmp_path / "_PD_1.CR3"
    raw.write_bytes(
        b"\x00\x00\x00\x18ftypcrx "
        + b"\x00" * 12
        + _box(b"CMT1", {272: "Canon EOS R6"})
        + _box(b"CMT2", {34855: 6400, 36867: "2026:10:24 19:46:27"})
    )

    assert read_cr3_exif(raw) == {
        "DateTimeOriginal": "2026:10:24 19:46:27",
        "Model": "Canon EOS R6",
        "ISOSpeedRatings": 6400,
    }


def test_read_cr3_exif_non_cr3_or_garbage_is_empty(tmp_path: Path):
    junk = tmp_path / "x.CR3"
    junk.write_bytes(b"not a cr3")
    other = tmp_path / "x.NEF"
    other.write_bytes(b"CMT2whatever")

    assert read_cr3_exif(junk) == {}
    assert read_cr3_exif(other) == {}
