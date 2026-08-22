from pathlib import Path

from PIL import Image

from shopsteward.editing.exifread import read_jpeg_exif, sibling_jpeg


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
