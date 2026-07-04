"""Mockup job orchestration: eligibility, selection wiring, idempotency,
--force, manual-drop photo_ref, and photo_id filtering."""

from pathlib import Path

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.jobs import run_mockups
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Isolate the operator templates dir so only the 4 committed placeholders
    # under config/defaults/staging_templates are in play, mirroring
    # tests/mockups/test_templates.py's conn fixture.
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _landing_master(path: Path, *, size: tuple[int, int] = (3600, 2400)) -> None:
    Image.new("RGB", size, (150, 130, 110)).save(path, format="TIFF")


def _observe_landing_file(conn, file_id: str, path: Path, *, photo_id: str | None = None) -> None:
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": str(path),
                "base_name": None,
                "format": "TIFF",
                "width": 3600,
                "height": 2400,
                "color_space": "RGB",
                "photo_id": photo_id,
            },
        ),
    )
    rebuild_pipeline(conn)


_ALL_INTENTS = (
    "single",
    "gallery_wall",
    "framed_poster",
    "canvas_edge",
    "acrylic",
    "digital_whatyougot",
)


def test_run_mockups_renders_every_enabled_intent(conn, tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    master_path = landing / "hero.tif"
    _landing_master(master_path)
    _observe_landing_file(conn, "a" * 64, master_path, photo_id="photo-001")

    out_dir = tmp_path / "mockups"
    result = run_mockups(conn, USER_ID, output_dir=out_dir)

    assert result.sets_completed == 1
    assert result.mockups_written >= len(_ALL_INTENTS)
    assert result.skipped_idempotent == 0
    assert result.templates_invalid == 0

    photo_dir = out_dir / "photo-001"
    files = sorted(p.name for p in photo_dir.iterdir())
    for intent in _ALL_INTENTS:
        assert any(f.startswith(f"{intent}_") for f in files), f"missing {intent} in {files}"

    generated = [e for e in read_all(conn, "mockup.generated") if e.user_id == USER_ID]
    assert len(generated) == result.mockups_written
    assert {e.payload["intent"] for e in generated} == set(_ALL_INTENTS)

    completed = [e for e in read_all(conn, "mockupset.completed") if e.user_id == USER_ID]
    assert len(completed) == 1
    assert completed[0].payload["count"] == result.mockups_written
    assert completed[0].payload["landing_file_id"] == "a" * 64


def test_run_mockups_is_idempotent_then_force_regenerates(conn, tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    master_path = landing / "hero.tif"
    _landing_master(master_path)
    _observe_landing_file(conn, "b" * 64, master_path, photo_id="photo-002")

    out_dir = tmp_path / "mockups"
    first = run_mockups(conn, USER_ID, output_dir=out_dir)
    assert first.mockups_written > 0

    second = run_mockups(conn, USER_ID, output_dir=out_dir)
    assert second.skipped_idempotent == 1
    assert second.mockups_written == 0
    assert second.sets_completed == 0

    forced = run_mockups(conn, USER_ID, output_dir=out_dir, force=True)
    assert forced.skipped_idempotent == 0
    assert forced.sets_completed == 1
    assert forced.mockups_written == first.mockups_written


def test_manual_drop_uses_file_prefixed_dir(conn, tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    master_path = landing / "unmatched.tif"
    _landing_master(master_path)
    file_id = "c" * 64
    _observe_landing_file(conn, file_id, master_path, photo_id=None)

    out_dir = tmp_path / "mockups"
    result = run_mockups(conn, USER_ID, output_dir=out_dir)

    assert result.sets_completed == 1
    expected_dir = out_dir / f"file-{file_id[:12]}"
    assert expected_dir.is_dir()
    assert any(expected_dir.iterdir())


def test_photo_id_filter_limits_to_one_landing_file(conn, tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()

    master_a = landing / "a.tif"
    master_b = landing / "b.tif"
    _landing_master(master_a)
    _landing_master(master_b)
    _observe_landing_file(conn, "d" * 64, master_a, photo_id="photo-a")
    _observe_landing_file(conn, "e" * 64, master_b, photo_id="photo-b")

    out_dir = tmp_path / "mockups"
    result = run_mockups(conn, USER_ID, photo_id="photo-a", output_dir=out_dir)

    assert result.sets_completed == 1
    assert (out_dir / "photo-a").is_dir()
    assert not (out_dir / "photo-b").exists()

    generated = [e for e in read_all(conn, "mockup.generated") if e.user_id == USER_ID]
    assert all(e.payload["photo_id"] == "photo-a" for e in generated)
