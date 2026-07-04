"""Landing-folder scan: technical validation + base-name match, idempotent."""

from pathlib import Path

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.editing.ingest import ingest_folder
from shopsteward.editing.projections import rebuild_editing
from shopsteward.pipeline import landing, tuning

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    tuning.seed(c, user_id=1, path=DEFAULTS_PATH)
    return c


def _ingest_hero_photo(conn, tmp_path, name: str) -> str:
    shoot = tmp_path / "shoot"
    shoot.mkdir(exist_ok=True)
    (shoot / f"{name}.CR3").write_bytes(f"raw-{name}".encode())
    Image.new("RGB", (20, 20)).save(shoot / f"{name}.jpg", "JPEG")
    report = ingest_folder(conn, user_id=1, path=shoot, mode="hero")
    rebuild_editing(conn)
    return report.photo_ids[0]


def test_valid_tiff_matches_ingested_photo(conn, tmp_path):
    _ingest_hero_photo(conn, tmp_path, "hero1")

    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    Image.new("RGB", (3500, 3600)).save(landing_dir / "hero1.tif", "TIFF")

    report = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)

    assert report.observed == 1
    assert report.matched == 1
    assert report.manual_drops == 0
    assert report.invalid == 0

    events = read_all(conn, "landing.file_observed")
    assert len(events) == 1
    assert events[0].payload["base_name"] == "hero1"
    assert events[0].payload["format"] == "TIFF"

    row = conn.execute(
        "SELECT * FROM proj_landing_files WHERE user_id=1 AND status='valid'"
    ).fetchone()
    assert row["base_name"] == "hero1"
    assert row["photo_id"] is not None


def test_manual_drop_for_unknown_name(conn, tmp_path):
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    Image.new("RGB", (3200, 3200)).save(landing_dir / "unrelated_shot.jpg", "JPEG")

    report = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)

    assert report.observed == 1
    assert report.matched == 0
    assert report.manual_drops == 1
    assert report.invalid == 0

    event = read_all(conn, "landing.file_observed")[0]
    assert event.payload["base_name"] is None
    assert event.payload["photo_id"] is None


def test_below_min_resolution_is_invalid(conn, tmp_path):
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    Image.new("RGB", (200, 200)).save(landing_dir / "tiny.jpg", "JPEG")

    report = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)

    assert report.observed == 0
    assert report.invalid == 1
    invalid_event = read_all(conn, "landing.file_invalid")[0]
    assert invalid_event.payload["reason"] == "below_min_resolution"


def test_junk_bytes_jpg_is_unreadable(conn, tmp_path):
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    (landing_dir / "junk.jpg").write_bytes(b"not a real jpeg at all")

    report = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)

    assert report.observed == 0
    assert report.invalid == 1
    invalid_event = read_all(conn, "landing.file_invalid")[0]
    assert invalid_event.payload["reason"] == "unreadable"


def test_png_is_ignored(conn, tmp_path):
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    Image.new("RGB", (3200, 3200)).save(landing_dir / "hero1.png", "PNG")

    report = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)

    assert report.observed == 0
    assert report.invalid == 0
    assert read_all(conn, "landing.file_observed") == []
    assert read_all(conn, "landing.file_invalid") == []


def test_second_scan_is_idempotent(conn, tmp_path):
    _ingest_hero_photo(conn, tmp_path, "hero1")
    landing_dir = tmp_path / "landing"
    landing_dir.mkdir()
    Image.new("RGB", (3500, 3600)).save(landing_dir / "hero1.tif", "TIFF")

    first = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)
    assert first.observed == 1

    second = landing.scan_landing(conn, user_id=1, landing_path=landing_dir)
    assert second.observed == 0
    assert len(read_all(conn, "landing.file_observed")) == 1
