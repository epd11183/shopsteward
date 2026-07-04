from io import BytesIO

from PIL import Image

from shopsteward.pipeline.imaging import prep_vision_jpeg


def _jpeg_with_exif(path, size) -> None:
    img = Image.new("RGB", size, (120, 80, 40))
    exif = Image.Exif()
    exif[271] = "TestMake"  # Make
    exif[306] = "2026:01:01 00:00:00"  # DateTime
    img.save(path, format="JPEG", exif=exif.tobytes())


def test_prep_vision_jpeg_downscales_landscape_and_strips_exif(tmp_path):
    src = tmp_path / "landscape.jpg"
    _jpeg_with_exif(src, (4000, 3000))

    result = prep_vision_jpeg(src, max_long_edge=1024)

    assert isinstance(result, bytes)
    out = Image.open(BytesIO(result))
    assert out.format == "JPEG"
    assert out.size == (1024, 768)
    assert len(out.getexif()) == 0
    assert "exif" not in out.info


def test_prep_vision_jpeg_downscales_portrait(tmp_path):
    src = tmp_path / "portrait.jpg"
    _jpeg_with_exif(src, (3000, 4000))

    result = prep_vision_jpeg(src, max_long_edge=1024)

    out = Image.open(BytesIO(result))
    assert out.size == (768, 1024)
    assert len(out.getexif()) == 0
