"""Full-flow E2E per design doc §7: zero-network run over a synthetic
non-8-bit-sRGB landing master + a controlled 2-template library (one
single-region, one 2-region gallery template) plus a concave-quad invalid
sidecar. Verifies all 6 intents produce output, pixel sanity of the
composited region vs untouched background, synthetic-render coverage,
whatyougot text presence, idempotency, --force, and invalid-template
exclusion.

Bit-depth note: the design calls for an AdobeRGB-tagged TIFF, falling back to
a 16-bit TIFF if constructing a real AdobeRGB ICC profile is awkward. PIL's
I;16 -> RGB conversion clamps rather than rescales (verified: any value above
255 saturates to 255), so this test exercises the 16-bit-mode code path in
compositor.prep_master (the ``img.mode not in ("RGB", "RGBA")`` branch) using
an in-range value rather than a realistic 16-bit dynamic range.
"""

import json
from pathlib import Path

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.mockups.jobs import run_mockups

USER_ID = 1

_TEMPLATE_IMG_SIZE = (1600, 1200)
_TEMPLATE_BG = (200, 200, 200)
_SINGLE_QUAD = [[300.0, 300.0], [1300.0, 320.0], [1290.0, 900.0], [310.0, 880.0]]
_GALLERY_QUAD_A = [[200.0, 300.0], [750.0, 310.0], [745.0, 900.0], [205.0, 890.0]]
_GALLERY_QUAD_B = [[900.0, 320.0], [1400.0, 325.0], [1396.0, 880.0], [904.0, 875.0]]
_CONCAVE_QUAD = [[100.0, 100.0], [700.0, 110.0], [110.0, 495.0], [690.0, 500.0]]

_ALL_INTENTS = (
    "single",
    "gallery_wall",
    "framed_poster",
    "canvas_edge",
    "acrylic",
    "digital_whatyougot",
)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Isolate the template registry to exactly the library this test builds:
    # no committed defaults, no operator dir, so template selection for
    # "single"/"framed_poster" is fully deterministic.
    monkeypatch.setattr(
        "shopsteward.mockups.templates.DEFAULT_TEMPLATES_DIR", tmp_path / "no_defaults"
    )
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _write_sidecar(lib: Path, template_id: str, *, regions: list[dict], orientation: str) -> None:
    lib.mkdir(parents=True, exist_ok=True)
    sidecar = {
        "schema": "shopsteward.stagingtemplate/1",
        "template_id": template_id,
        "room_type": template_id,  # distinct room_type per template avoids diversity relax
        "style": "modern",
        "lighting": "neutral",
        "orientation": orientation,
        "regions": regions,
        "tags": [],
    }
    (lib / f"{template_id}.template.json").write_text(json.dumps(sidecar))


def _build_library(lib: Path) -> None:
    _write_sidecar(
        lib,
        "e2e-single-01",
        regions=[{"kind": "wall_print", "quad": _SINGLE_QUAD, "region_width_inches": 30.0}],
        orientation="landscape",
    )
    Image.new("RGB", _TEMPLATE_IMG_SIZE, _TEMPLATE_BG).save(lib / "e2e-single-01.jpg")

    _write_sidecar(
        lib,
        "e2e-gallery-01",
        regions=[
            {"kind": "wall_print", "quad": _GALLERY_QUAD_A, "region_width_inches": 24.0},
            {"kind": "wall_print", "quad": _GALLERY_QUAD_B, "region_width_inches": 16.0},
        ],
        orientation="landscape",
    )
    Image.new("RGB", _TEMPLATE_IMG_SIZE, _TEMPLATE_BG).save(lib / "e2e-gallery-01.jpg")

    _write_sidecar(
        lib,
        "e2e-concave-01",
        regions=[{"kind": "wall_print", "quad": _CONCAVE_QUAD, "region_width_inches": 30.0}],
        orientation="landscape",
    )
    Image.new("RGB", (800, 600), _TEMPLATE_BG).save(lib / "e2e-concave-01.jpg")


def _landing_master_16bit(path: Path, *, size: tuple[int, int] = (3600, 2400)) -> None:
    """16-bit single-channel TIFF, dark value for strong contrast against the
    template background -- see module docstring for the clamp-vs-rescale
    caveat this implies."""
    Image.new("I;16", size, 40).save(path, format="TIFF")


def _final_scale(template_long_edge: int, output_long_edge_px: int) -> float:
    return output_long_edge_px / template_long_edge


def test_full_flow_pixel_sanity_and_exclusions(conn, tmp_path):
    lib = tmp_path / "lib"
    _build_library(lib)

    from shopsteward.core.events import Event, append
    from shopsteward.pipeline.projections import rebuild_pipeline

    landing = tmp_path / "landing"
    landing.mkdir()
    master_path = landing / "hero.tif"
    _landing_master_16bit(master_path)

    file_id = "f" * 64
    photo_id = "photo-e2e"
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": str(master_path),
                "base_name": None,
                "format": "TIFF",
                "width": 3600,
                "height": 2400,
                "color_space": "I;16",
                "photo_id": photo_id,
            },
        ),
    )
    rebuild_pipeline(conn)

    out_dir = tmp_path / "mockups"
    result = run_mockups(conn, USER_ID, extra_template_dirs=[lib], output_dir=out_dir)

    # -- concave-quad sidecar excluded --
    invalid_events = [
        e
        for e in read_all(conn, "stagingtemplate.invalid")
        if e.payload.get("template_id") == "e2e-concave-01"
    ]
    assert len(invalid_events) == 1
    assert invalid_events[0].payload["reason"] == "quad_concave"
    row = conn.execute(
        "SELECT status FROM proj_staging_templates WHERE user_id=? AND template_id=?",
        (USER_ID, "e2e-concave-01"),
    ).fetchone()
    assert row["status"] == "invalid"
    assert result.templates_invalid == 1

    # -- all 6 intents produced at least one file; the concave template never
    # appears in any output filename --
    photo_dir = out_dir / photo_id
    files = sorted(p.name for p in photo_dir.iterdir())
    for intent in _ALL_INTENTS:
        assert any(f.startswith(f"{intent}_") for f in files), f"missing {intent} in {files}"
    assert not any("e2e-concave-01" in f for f in files)

    generated = [e for e in read_all(conn, "mockup.generated") if e.user_id == USER_ID]
    assert len(generated) == result.mockups_written
    assert {e.payload["intent"] for e in generated} == set(_ALL_INTENTS)

    # -- pixel sanity: single_e2e-single-01.jpg --
    single_path = photo_dir / "single_e2e-single-01.jpg"
    assert single_path.is_file()
    single_img = Image.open(single_path).convert("RGB")

    scale = _final_scale(max(_TEMPLATE_IMG_SIZE), 2400)
    xs = [p[0] for p in _SINGLE_QUAD]
    ys = [p[1] for p in _SINGLE_QUAD]
    centroid = (sum(xs) / len(xs) * scale, sum(ys) / len(ys) * scale)
    centroid_px = single_img.getpixel((int(centroid[0]), int(centroid[1])))
    centroid_delta = max(abs(c - b) for c, b in zip(centroid_px, _TEMPLATE_BG, strict=True))
    assert centroid_delta > 25, f"region centroid barely changed: {centroid_px}"

    outside_px = single_img.getpixel((int(50 * scale), int(50 * scale)))
    outside_delta = max(abs(c - b) for c, b in zip(outside_px, _TEMPLATE_BG, strict=True))
    assert outside_delta <= 6, f"outside-quad pixel drifted too far: {outside_px}"

    # -- synthetic renders have meaningful non-backdrop coverage --
    for intent, backdrop in (("canvas_edge", (240, 239, 236)), ("acrylic", (250, 250, 250))):
        synthetic_path = photo_dir / f"{intent}_synthetic.jpg"
        assert synthetic_path.is_file()
        img = Image.open(synthetic_path).convert("RGB")
        pixels = list(img.get_flattened_data())
        non_backdrop = sum(
            1 for px in pixels if max(abs(c - b) for c, b in zip(px, backdrop, strict=True)) > 10
        )
        fraction = non_backdrop / len(pixels)
        assert fraction > 0.05, f"{intent} looks like a blank backdrop ({fraction:.3%})"

    # -- whatyougot panel has text/thumb pixels (not a blank white panel) --
    whatyougot_path = photo_dir / "digital_whatyougot_synthetic.jpg"
    assert whatyougot_path.is_file()
    wyg = Image.open(whatyougot_path).convert("RGB")
    dark_pixels = sum(1 for px in wyg.get_flattened_data() if min(px) < 200)
    assert dark_pixels > 0, "whatyougot panel appears blank"


def test_full_flow_idempotent_then_force(conn, tmp_path):
    lib = tmp_path / "lib"
    _build_library(lib)

    from shopsteward.core.events import Event, append
    from shopsteward.pipeline.projections import rebuild_pipeline

    landing = tmp_path / "landing"
    landing.mkdir()
    master_path = landing / "hero.tif"
    _landing_master_16bit(master_path)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": "g" * 64,
                "path": str(master_path),
                "base_name": None,
                "format": "TIFF",
                "width": 3600,
                "height": 2400,
                "color_space": "I;16",
                "photo_id": "photo-e2e-2",
            },
        ),
    )
    rebuild_pipeline(conn)

    out_dir = tmp_path / "mockups"
    first = run_mockups(conn, USER_ID, extra_template_dirs=[lib], output_dir=out_dir)
    assert first.mockups_written > 0

    second = run_mockups(conn, USER_ID, extra_template_dirs=[lib], output_dir=out_dir)
    assert second.skipped_idempotent == 1
    assert second.mockups_written == 0

    forced = run_mockups(conn, USER_ID, extra_template_dirs=[lib], output_dir=out_dir, force=True)
    assert forced.mockups_written == first.mockups_written
