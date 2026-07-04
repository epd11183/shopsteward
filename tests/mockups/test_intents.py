"""Per-intent renderer tests: output dims, template-backed pixel sanity
(region centroid shifts, outside-quad stays near background), synthetic
render sanity (non-backdrop fraction, text pixels), gallery-wall multi-region
compositing, and companion_photos plumbing."""

import numpy as np
import pytest
from PIL import Image

from shopsteward.mockups.intents import render_intent
from tests.mockups.helpers import (
    flat_template_rect,
    make_cfg,
    make_region,
    make_template,
    random_photo,
    solid_photo,
)


def test_single_intent_dims_and_pixel_sanity():
    cfg = make_cfg()
    template_img = flat_template_rect(1600, 1200, (120, 120, 120))
    region = make_region([[300, 200], [1300, 215], [1290, 800], [310, 785]], width_inches=36)
    template = make_template("single-test", [region])
    photo = solid_photo(1600, 1200, color=(210, 40, 40))

    out_img, params = render_intent("single", photo, template, template_img, cfg)

    assert isinstance(out_img, Image.Image)
    assert max(out_img.size) == cfg.render.output_long_edge_px
    assert params["intent"] == "single"
    assert params["template_id"] == "single-test"
    assert "gain" in params and "print_w_in" in params

    out_arr = np.asarray(out_img).astype(int)
    scale = cfg.render.output_long_edge_px / 1600
    cx = int(((300 + 1300 + 1290 + 310) / 4) * scale)
    cy = int(((200 + 215 + 800 + 785) / 4) * scale)
    centroid_diff = np.abs(out_arr[cy, cx] - np.array([120, 120, 120])).sum()
    assert centroid_diff > 25

    outside_diff = np.abs(out_arr[5, 5] - np.array([120, 120, 120])).sum()
    assert outside_diff <= 6


def test_framed_poster_dims_and_params():
    cfg = make_cfg()
    template_img = flat_template_rect(1600, 1200, (200, 200, 200))
    region = make_region([[300, 200], [1300, 215], [1290, 800], [310, 785]], width_inches=36)
    template = make_template("framed-test", [region])
    photo = random_photo(1600, 1200, seed=1)

    out_img, params = render_intent("framed_poster", photo, template, template_img, cfg)

    assert max(out_img.size) == cfg.render.output_long_edge_px
    assert params["intent"] == "framed_poster"
    assert params["template_id"] == "framed-test"
    assert "gain" in params


def test_gallery_wall_both_regions_change_vs_template():
    cfg = make_cfg()
    template_img = flat_template_rect(1600, 1200, (150, 150, 150))
    region_a = make_region([[200, 250], [750, 260], [745, 750], [205, 740]], width_inches=24)
    region_b = make_region([[850, 270], [1250, 275], [1246, 730], [854, 725]], width_inches=16)
    template = make_template("gallery-test", [region_a, region_b])
    photo = solid_photo(1600, 1200, color=(210, 40, 40))

    out_img, params = render_intent("gallery_wall", photo, template, template_img, cfg)

    assert params["intent"] == "gallery_wall"
    assert params["template_id"] == "gallery-test"
    assert len(params["crops"]) == 2

    out_arr = np.asarray(out_img).astype(int)
    scale = cfg.render.output_long_edge_px / 1600

    for region in (region_a, region_b):
        pts = np.array(region.quad)
        cx, cy = (pts.mean(axis=0) * scale).astype(int)
        diff = np.abs(out_arr[cy, cx] - np.array([150, 150, 150])).sum()
        assert diff > 25


def test_gallery_wall_uses_companion_photo_when_provided():
    cfg = make_cfg()
    template_img = flat_template_rect(1600, 1200, (150, 150, 150))
    region_a = make_region([[200, 250], [750, 260], [745, 750], [205, 740]], width_inches=24)
    region_b = make_region([[850, 270], [1250, 275], [1246, 730], [854, 725]], width_inches=16)
    template = make_template("gallery-companion-test", [region_a, region_b])
    photo = random_photo(1600, 1200, seed=6)
    companion = random_photo(400, 300, seed=7)

    _out_img, params = render_intent(
        "gallery_wall", photo, template, template_img, cfg, companion_photos=[companion]
    )

    assert params["crops"][1]["crop"] == "companion"


def test_canvas_edge_dims_and_nonbackdrop_fraction():
    cfg = make_cfg()
    photo = random_photo(1600, 1200, seed=3)

    out_img, params = render_intent("canvas_edge", photo, None, None, cfg)

    assert out_img.size == (2400, 1800)
    assert params["intent"] == "canvas_edge"
    assert params["template_id"] is None

    arr = np.asarray(out_img).astype(int)
    backdrop = np.array([240, 239, 236])
    diff = np.abs(arr - backdrop).sum(axis=2)
    assert (diff > 10).mean() > 0.05


def test_acrylic_dims_and_nonbackdrop_fraction():
    cfg = make_cfg()
    photo = random_photo(1600, 1200, seed=4)

    out_img, params = render_intent("acrylic", photo, None, None, cfg)

    assert out_img.size == (2400, 1800)
    assert params["intent"] == "acrylic"
    assert params["template_id"] is None

    arr = np.asarray(out_img).astype(int)
    backdrop = np.array([250, 250, 250])
    diff = np.abs(arr - backdrop).sum(axis=2)
    assert (diff > 10).mean() > 0.05


def test_digital_whatyougot_dims_and_text_present():
    cfg = make_cfg()
    photo = random_photo(1600, 1200, seed=5)

    out_img, params = render_intent("digital_whatyougot", photo, None, None, cfg)

    assert out_img.size == (2400, 2400)
    assert params["intent"] == "digital_whatyougot"
    assert params["template_id"] is None

    gray = np.asarray(out_img.convert("L"))
    dark_pixel_count = int((gray < 100).sum())
    assert dark_pixel_count > 500


def test_render_intent_rejects_unknown_intent():
    cfg = make_cfg()
    photo = random_photo(100, 100, seed=8)
    with pytest.raises(ValueError, match="not_a_real_intent"):
        render_intent("not_a_real_intent", photo, None, None, cfg)
