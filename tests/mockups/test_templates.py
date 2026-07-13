"""Staging template registry scan: validation matrix, idempotency, avg_hue,
write_sidecar guard, and the committed synthetic placeholders."""

import json
from pathlib import Path

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.mockups.models import StagingTemplate
from shopsteward.mockups.templates import scan_templates, write_sidecar

USER_ID = 1

_DEFAULT_QUAD = [[100.0, 100.0], [700.0, 110.0], [690.0, 500.0], [110.0, 495.0]]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Isolate the operator templates dir so it's always empty/absent -- only
    # config/defaults/staging_templates (the 4 committed placeholders) plus
    # whatever extra_dirs a test passes in are scanned.
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _events_for(conn, type_prefix: str, template_id: str) -> list:
    return [e for e in read_all(conn, type_prefix) if e.payload["template_id"] == template_id]


def _write_pair(
    dir_path: Path,
    template_id: str,
    *,
    image_size: tuple[int, int] = (800, 600),
    regions: list[dict] | None = None,
    orientation: str = "landscape",
    color: tuple[int, int, int] = (120, 140, 160),
    image_suffix: str = ".jpg",
    write_image: bool = True,
) -> tuple[Path, Path | None]:
    dir_path.mkdir(parents=True, exist_ok=True)
    if regions is None:
        regions = [{"kind": "wall_print", "quad": _DEFAULT_QUAD, "region_width_inches": 30.0}]

    sidecar = {
        "schema": "shopsteward.stagingtemplate/1",
        "template_id": template_id,
        "room_type": "living_room",
        "style": "modern",
        "lighting": "warm_daylight",
        "orientation": orientation,
        "regions": regions,
        "tags": ["neutral_wall"],
    }
    sidecar_path = dir_path / f"{template_id}.template.json"
    sidecar_path.write_text(json.dumps(sidecar))

    image_path = None
    if write_image:
        image_path = dir_path / f"{template_id}{image_suffix}"
        Image.new("RGB", image_size, color).save(image_path)

    return sidecar_path, image_path


def test_valid_template_registers(conn, tmp_path):
    lib = tmp_path / "lib"
    _write_pair(lib, "valid-01")

    report = scan_templates(conn, USER_ID, extra_dirs=[lib])

    assert report.invalid == 0
    events = _events_for(conn, "stagingtemplate.registered", "valid-01")
    assert len(events) == 1
    payload = events[0].payload
    assert 0.0 <= payload["avg_hue"] < 360.0
    assert payload["region_count"] == 1
    assert payload["source"] == "operator"

    row = conn.execute(
        "SELECT * FROM proj_staging_templates WHERE user_id=? AND template_id=?",
        (USER_ID, "valid-01"),
    ).fetchone()
    assert row["status"] == "valid"


def test_concave_quad_is_invalid(conn, tmp_path):
    lib = tmp_path / "lib"
    concave_quad = [[100.0, 100.0], [700.0, 110.0], [110.0, 495.0], [690.0, 500.0]]  # BR/BL swapped
    _write_pair(
        lib,
        "concave-01",
        regions=[{"kind": "wall_print", "quad": concave_quad, "region_width_inches": 30.0}],
    )

    scan_templates(conn, USER_ID, extra_dirs=[lib])

    events = _events_for(conn, "stagingtemplate.invalid", "concave-01")
    assert len(events) == 1
    assert events[0].payload["reason"] == "quad_concave"


def test_out_of_bounds_quad_is_invalid(conn, tmp_path):
    lib = tmp_path / "lib"
    quad = [[100.0, 100.0], [900.0, 110.0], [890.0, 500.0], [110.0, 495.0]]  # x=900 > width 800
    _write_pair(
        lib,
        "oob-01",
        image_size=(800, 600),
        regions=[{"kind": "wall_print", "quad": quad, "region_width_inches": 30.0}],
    )

    scan_templates(conn, USER_ID, extra_dirs=[lib])

    events = _events_for(conn, "stagingtemplate.invalid", "oob-01")
    assert len(events) == 1
    assert events[0].payload["reason"] == "quad_out_of_bounds"


def test_tiny_quad_is_invalid(conn, tmp_path):
    lib = tmp_path / "lib"
    tiny_quad = [[100.0, 100.0], [150.0, 100.0], [150.0, 150.0], [100.0, 150.0]]
    _write_pair(
        lib,
        "tiny-01",
        image_size=(800, 600),
        regions=[{"kind": "wall_print", "quad": tiny_quad, "region_width_inches": 30.0}],
    )

    scan_templates(conn, USER_ID, extra_dirs=[lib])

    events = _events_for(conn, "stagingtemplate.invalid", "tiny-01")
    assert len(events) == 1
    assert events[0].payload["reason"] == "quad_too_small"


def test_ppi_out_of_band_is_invalid(conn, tmp_path):
    lib = tmp_path / "lib"
    # top edge ~100px over 120in of width -> implied ppi < 10.
    quad = [[100.0, 100.0], [200.0, 100.0], [195.0, 300.0], [105.0, 300.0]]
    _write_pair(
        lib,
        "lowppi-01",
        image_size=(800, 600),
        regions=[{"kind": "wall_print", "quad": quad, "region_width_inches": 120.0}],
    )

    scan_templates(conn, USER_ID, extra_dirs=[lib])

    events = _events_for(conn, "stagingtemplate.invalid", "lowppi-01")
    assert len(events) == 1
    assert events[0].payload["reason"] == "implied_ppi_out_of_range"


def test_duplicate_id_across_dirs_invalidates_both(conn, tmp_path):
    lib_a = tmp_path / "lib_a"
    lib_b = tmp_path / "lib_b"
    _write_pair(lib_a, "dup-01")
    _write_pair(lib_b, "dup-01")

    scan_templates(conn, USER_ID, extra_dirs=[lib_a, lib_b])

    events = _events_for(conn, "stagingtemplate.invalid", "dup-01")
    assert len(events) == 2
    assert all(e.payload["reason"] == "duplicate_id" for e in events)


def test_missing_image_is_invalid(conn, tmp_path):
    lib = tmp_path / "lib"
    _write_pair(lib, "noimage-01", write_image=False)

    scan_templates(conn, USER_ID, extra_dirs=[lib])

    events = _events_for(conn, "stagingtemplate.invalid", "noimage-01")
    assert len(events) == 1
    assert events[0].payload["reason"] == "image_missing"


def test_scan_is_idempotent(conn, tmp_path):
    lib = tmp_path / "lib"
    _write_pair(lib, "idem-01")

    first = scan_templates(conn, USER_ID, extra_dirs=[lib])
    events_after_first = read_all(conn)

    second = scan_templates(conn, USER_ID, extra_dirs=[lib])
    events_after_second = read_all(conn)

    assert first.registered >= 1
    assert second.registered == 0
    assert second.invalid == 0
    assert second.unchanged == first.registered
    assert len(events_after_second) == len(events_after_first)


def test_sidecar_edit_produces_updated_event(conn, tmp_path):
    lib = tmp_path / "lib"
    sidecar_path, _ = _write_pair(lib, "edit-01")

    scan_templates(conn, USER_ID, extra_dirs=[lib])

    data = json.loads(sidecar_path.read_text())
    data["tags"] = ["neutral_wall", "plants"]
    sidecar_path.write_text(json.dumps(data))

    report = scan_templates(conn, USER_ID, extra_dirs=[lib])

    assert report.updated == 1
    updated_events = _events_for(conn, "stagingtemplate.updated", "edit-01")
    assert len(updated_events) == 1


def test_write_sidecar_refuses_outside_allowed_dirs(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    image_path = outside / "sneaky.jpg"
    Image.new("RGB", (100, 100)).save(image_path)

    template = StagingTemplate(
        schema="shopsteward.stagingtemplate/1",
        template_id="sneaky-01",
        room_type="living_room",
        style="modern",
        lighting="warm_daylight",
        orientation="landscape",
        regions=[{"kind": "wall_print", "quad": _DEFAULT_QUAD, "region_width_inches": 30.0}],
        tags=[],
    )

    with pytest.raises(ValueError):
        write_sidecar(image_path, template, allowed_dirs=[allowed])


def test_write_sidecar_then_scan_registers(conn, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    image_path = lib / "written-01.jpg"
    Image.new("RGB", (800, 600), (100, 120, 140)).save(image_path)

    template = StagingTemplate(
        schema="shopsteward.stagingtemplate/1",
        template_id="written-01",
        room_type="living_room",
        style="modern",
        lighting="warm_daylight",
        orientation="landscape",
        regions=[{"kind": "wall_print", "quad": _DEFAULT_QUAD, "region_width_inches": 30.0}],
        tags=["neutral_wall"],
    )

    sidecar_path = write_sidecar(image_path, template, allowed_dirs=[lib])
    assert sidecar_path == lib / "written-01.template.json"

    report = scan_templates(conn, USER_ID, extra_dirs=[lib])
    assert report.invalid == 0
    events = _events_for(conn, "stagingtemplate.registered", "written-01")
    assert len(events) == 1


def test_committed_defaults_all_scan_valid(conn):
    report = scan_templates(conn, USER_ID)

    assert report.invalid == 0
    assert report.registered >= 4

    rows = conn.execute(
        "SELECT template_id, region_count, status FROM proj_staging_templates WHERE user_id=?",
        (USER_ID,),
    ).fetchall()
    assert len(rows) == report.registered
    assert all(row["status"] == "valid" for row in rows)
    # gallery_wall needs at least one multi-region template in the library
    assert any(row["region_count"] >= 2 for row in rows)
