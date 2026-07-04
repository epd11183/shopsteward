from pathlib import Path

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.editing.ingest import ingest_folder
from shopsteward.editing.projections import rebuild_editing


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _make_jpeg(path: Path) -> None:
    Image.new("RGB", (8, 8)).save(path, "JPEG")


def _make_raw(path: Path, content: bytes = b"raw-bytes") -> None:
    path.write_bytes(content)


def test_pairs_and_reports_orphans(conn, tmp_path):
    folder = tmp_path / "shoot"
    folder.mkdir()
    for name in ("a", "b", "c"):
        _make_raw(folder / f"{name}.CR3", content=f"raw-{name}".encode())
        _make_jpeg(folder / f"{name}.jpg")
    _make_jpeg(folder / "orphan_jpeg.jpg")
    _make_raw(folder / "orphan_raw.CR3", content=b"raw-orphan")

    report = ingest_folder(conn, user_id=1, path=folder, mode="mass")

    assert report.paired == 3
    assert report.unpaired == 2
    assert report.duplicates == 0
    assert len(report.photo_ids) == 3

    ingested = read_all(conn, "photo.ingested")
    assert len(ingested) == 3
    assert all(e.payload["status"] == "queued_for_edit" for e in ingested)

    unpaired_events = read_all(conn, "photo.unpaired")
    assert len(unpaired_events) == 2
    reasons = {e.payload["reason"] for e in unpaired_events}
    assert reasons == {"missing_jpeg", "missing_raw"}


def test_reingest_same_folder_reports_duplicates(conn, tmp_path):
    folder = tmp_path / "shoot"
    folder.mkdir()
    for name in ("a", "b", "c"):
        _make_raw(folder / f"{name}.CR3", content=f"raw-{name}".encode())
        _make_jpeg(folder / f"{name}.jpg")

    ingest_folder(conn, user_id=1, path=folder, mode="mass")
    second = ingest_folder(conn, user_id=1, path=folder, mode="mass")

    assert second.paired == 0
    assert second.duplicates == 3


def test_case_insensitive_pairing(conn, tmp_path):
    folder = tmp_path / "shoot"
    folder.mkdir()
    _make_raw(folder / "IMG_1.CR3", content=b"raw-img-1")
    _make_jpeg(folder / "img_1.JPG")

    report = ingest_folder(conn, user_id=1, path=folder, mode="mass")

    assert report.paired == 1
    assert report.unpaired == 0


def test_corrupt_jpeg_still_ingests_with_empty_exif(conn, tmp_path):
    folder = tmp_path / "shoot"
    folder.mkdir()
    _make_raw(folder / "a.CR3", content=b"raw-a")
    (folder / "a.jpg").write_bytes(b"not a real jpeg")

    report = ingest_folder(conn, user_id=1, path=folder, mode="mass")

    assert report.paired == 1
    [event] = read_all(conn, "photo.ingested")
    assert event.payload["exif"] == {}


def test_empty_folder_produces_zero_report_with_lifecycle_events(conn, tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()

    report = ingest_folder(conn, user_id=1, path=folder, mode="mass")

    assert (report.paired, report.duplicates, report.unpaired) == (0, 0, 0)
    assert len(read_all(conn, "ingest.started")) == 1
    assert len(read_all(conn, "ingest.completed")) == 1


def test_hero_mode_status_is_awaiting_scoring(conn, tmp_path):
    folder = tmp_path / "shoot"
    folder.mkdir()
    _make_raw(folder / "a.CR3", content=b"raw-a")
    _make_jpeg(folder / "a.jpg")

    ingest_folder(conn, user_id=1, path=folder, mode="hero")
    rebuild_editing(conn)

    status = conn.execute("SELECT status FROM proj_photos WHERE user_id=1").fetchone()["status"]
    assert status == "awaiting_scoring"
