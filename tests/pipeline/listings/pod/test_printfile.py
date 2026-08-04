import hashlib

import pytest
from PIL import Image

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.pod import printfile
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _observe(conn, *, file_id, path, base_name, photo_id, fmt, width=4000, height=3000):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": str(path),
                "base_name": base_name,
                "format": fmt,
                "width": width,
                "height": height,
                "color_space": "sRGB",
                "photo_id": photo_id,
            },
        ),
    )
    rebuild_pipeline(conn)


# --- resolve_print_source_path ----------------------------------------------


def test_resolve_prefers_own_path_when_already_the_preferred_format(conn, tmp_path):
    tiff_path = tmp_path / "hero.tif"
    Image.new("RGB", (10, 10)).save(tiff_path, "TIFF")
    _observe(conn, file_id="f1", path=tiff_path, base_name="hero", photo_id="p1", fmt="TIFF")

    resolved = printfile.resolve_print_source_path(conn, USER_ID, "f1", "tiff_master")
    assert resolved == str(tiff_path)


def test_resolve_finds_a_tiff_sibling_by_base_name(conn, tmp_path):
    tiff_path = tmp_path / "hero.tif"
    jpeg_path = tmp_path / "hero.jpg"
    Image.new("RGB", (10, 10)).save(tiff_path, "TIFF")
    Image.new("RGB", (10, 10)).save(jpeg_path, "JPEG")
    _observe(conn, file_id="f-tiff", path=tiff_path, base_name="hero", photo_id="p1", fmt="TIFF")
    _observe(conn, file_id="f-jpeg", path=jpeg_path, base_name="hero", photo_id="p1", fmt="JPEG")

    # Processing the JPEG row but preferring the TIFF master -- must resolve
    # to the sibling TIFF's path, not the JPEG's own.
    resolved = printfile.resolve_print_source_path(conn, USER_ID, "f-jpeg", "tiff_master")
    assert resolved == str(tiff_path)


def test_resolve_falls_back_to_own_path_when_no_tiff_sibling_exists(conn, tmp_path):
    jpeg_path = tmp_path / "hero.jpg"
    Image.new("RGB", (10, 10)).save(jpeg_path, "JPEG")
    _observe(conn, file_id="f-jpeg", path=jpeg_path, base_name="hero", photo_id="p1", fmt="JPEG")

    resolved = printfile.resolve_print_source_path(conn, USER_ID, "f-jpeg", "tiff_master")
    assert resolved == str(jpeg_path)


def test_resolve_jpeg_preference_uses_own_jpeg_row(conn, tmp_path):
    tiff_path = tmp_path / "hero.tif"
    jpeg_path = tmp_path / "hero.jpg"
    Image.new("RGB", (10, 10)).save(tiff_path, "TIFF")
    Image.new("RGB", (10, 10)).save(jpeg_path, "JPEG")
    _observe(conn, file_id="f-tiff", path=tiff_path, base_name="hero", photo_id="p1", fmt="TIFF")
    _observe(conn, file_id="f-jpeg", path=jpeg_path, base_name="hero", photo_id="p1", fmt="JPEG")

    resolved = printfile.resolve_print_source_path(conn, USER_ID, "f-tiff", "jpeg")
    assert resolved == str(jpeg_path)


def test_resolve_unknown_landing_file_id_raises(conn):
    rebuild_pipeline(conn)  # proj_landing_files only exists after a rebuild
    with pytest.raises(LookupError):
        printfile.resolve_print_source_path(conn, USER_ID, "nope", "tiff_master")


# --- prepare_print_file -------------------------------------------------------


def test_prepare_print_file_reencodes_a_tiff_to_a_derived_jpeg(conn, tmp_path):
    tiff_path = tmp_path / "hero.tif"
    Image.new("RGB", (20, 20)).save(tiff_path, "TIFF")
    _observe(conn, file_id="f1", path=tiff_path, base_name="hero", photo_id="p1", fmt="TIFF")

    data, sellable = printfile.prepare_print_file(conn, USER_ID, "f1", "tiff_master", 100_000_000)
    assert sellable.source == "derived_jpeg"
    assert sellable.sha256 == hashlib.sha256(data).hexdigest()


def test_prepare_print_file_keeps_a_small_jpeg_as_landing_original(conn, tmp_path):
    jpeg_path = tmp_path / "hero.jpg"
    Image.new("RGB", (20, 20)).save(jpeg_path, "JPEG")
    _observe(conn, file_id="f1", path=jpeg_path, base_name="hero", photo_id="p1", fmt="JPEG")

    data, sellable = printfile.prepare_print_file(conn, USER_ID, "f1", "jpeg", 100_000_000)
    assert sellable.source == "landing_original"
    assert data == jpeg_path.read_bytes()


# --- publish_print_file -------------------------------------------------------


def test_publish_print_file_names_the_object_after_its_sha256(conn, tmp_path):
    jpeg_path = tmp_path / "hero.jpg"
    Image.new("RGB", (20, 20)).save(jpeg_path, "JPEG")
    _observe(conn, file_id="f1", path=jpeg_path, base_name="hero", photo_id="p1", fmt="JPEG")
    data, sellable = printfile.prepare_print_file(conn, USER_ID, "f1", "jpeg", 100_000_000)

    host = FakePrintFileHost()
    hosted = printfile.publish_print_file(host, data, sellable, ttl_seconds=3600)

    assert hosted.key == sellable.sha256
    assert host.calls[0] == (
        "publish",
        {"key": sellable.sha256, "name": sellable.sha256, "ttl_seconds": 3600, "bytes": len(data)},
    )
