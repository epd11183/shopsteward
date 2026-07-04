"""Compositor pure-function tests: geometry (ppi, scale-fit, target_quad),
warp correctness, light-match clamps, mat/frame borders, prep_master."""

import numpy as np
import pytest
from PIL import Image as PILImage

from shopsteward.mockups.compositor import (
    apply_mat,
    composite_print,
    draw_frame_border,
    finalize,
    light_stats,
    photo_orientation,
    prep_master,
    print_rect_inches,
    region_ppi,
    target_quad,
)
from tests.mockups.helpers import checkerboard, flat_template, make_cfg, make_region


def test_region_ppi_axis_aligned():
    region = make_region([[0, 0], [400, 0], [400, 400], [0, 400]], width_inches=20)
    assert region_ppi(region) == pytest.approx(20.0)


def test_photo_orientation_classification():
    assert photo_orientation(1000, 600) == "landscape"
    assert photo_orientation(600, 1000) == "portrait"
    assert photo_orientation(1000, 1000) == "square"
    assert photo_orientation(1030, 1000) == "square"  # within 5% tolerance
    assert photo_orientation(1200, 1000) == "landscape"  # outside tolerance


def test_print_rect_inches_uses_default_width_when_region_has_room():
    cfg = make_cfg()
    region = make_region([[0, 0], [720, 0], [720, 900], [0, 900]], width_inches=36)  # 20 ppi
    w_in, h_in = print_rect_inches(2000, 1200, region, cfg)  # landscape photo
    assert w_in == pytest.approx(24.0)
    assert h_in == pytest.approx(24.0 * 1200 / 2000)


def test_print_rect_inches_scales_down_when_region_too_small():
    cfg = make_cfg()
    region = make_region([[0, 0], [200, 0], [200, 250], [0, 250]], width_inches=10)
    w_in, h_in = print_rect_inches(2000, 1200, region, cfg)
    bound_w = 10 * (1 - 2 * cfg.render.mat_fraction)
    assert w_in <= bound_w + 1e-6
    assert w_in / h_in == pytest.approx(2000 / 1200)  # aspect preserved while scaling


def test_fit_never_crop_preserves_aspect_for_portrait_into_wide_region():
    cfg = make_cfg()
    region = make_region([[0, 0], [3000, 0], [3000, 600], [0, 600]], width_inches=100)
    photo_w, photo_h = 1200, 1800  # portrait
    w_in, h_in = print_rect_inches(photo_w, photo_h, region, cfg)
    assert w_in / h_in == pytest.approx(photo_w / photo_h, rel=1e-6)


def test_scale_print_plus_mat_width_matches_expected_px_within_5pct():
    """region 36in wide, top edge 720px (20ppi); landscape photo, default
    width 24in -> warped print+mat width along top ~= (24 + 2*mat) * 20 px."""
    cfg = make_cfg()
    region = make_region([[0, 0], [720, 0], [720, 900], [0, 900]], width_inches=36)
    photo_w, photo_h = 2000, 1200

    w_in, h_in = print_rect_inches(photo_w, photo_h, region, cfg)
    assert w_in == pytest.approx(24.0)

    mat_frac = cfg.render.mat_fraction
    mat_border_in = mat_frac * min(w_in, h_in)
    total_w_in = w_in + 2 * mat_border_in
    total_h_in = h_in + 2 * mat_border_in

    ppi = region_ppi(region)
    expected_px = total_w_in * ppi

    quad = target_quad(region, total_w_in, total_h_in)
    top_width_px = float(np.linalg.norm(quad[1] - quad[0]))

    assert top_width_px == pytest.approx(expected_px, rel=0.05)


def test_target_quad_centers_within_region():
    region = make_region([[100, 100], [500, 100], [500, 500], [100, 500]], width_inches=20)
    quad = target_quad(region, w_in=10, h_in=10)
    assert quad[:, 0].mean() == pytest.approx(300, abs=2)
    assert quad[:, 1].mean() == pytest.approx(300, abs=2)


def test_light_stats_dark_neighborhood_reduces_gain_but_respects_floor():
    cfg = make_cfg()
    template_img = flat_template(size=1000, color=(10, 10, 10))
    region = make_region([[400, 400], [600, 400], [600, 600], [400, 600]], width_inches=10)
    quad = np.array(region.quad, dtype=float)

    gain, _wb_r, _wb_b = light_stats(template_img, quad, cfg)
    assert gain < 1.0
    assert gain >= cfg.render.light_match.gain_min


def test_light_stats_white_neighborhood_hits_gain_max_clamp():
    cfg = make_cfg()
    template_img = flat_template(size=1000, color=(255, 255, 255))
    region = make_region([[400, 400], [600, 400], [600, 600], [400, 600]], width_inches=10)
    quad = np.array(region.quad, dtype=float)

    gain, _wb_r, _wb_b = light_stats(template_img, quad, cfg)
    assert gain == pytest.approx(cfg.render.light_match.gain_max)


def test_light_stats_empty_neighborhood_is_noop():
    cfg = make_cfg()
    template_img = flat_template(size=200, color=(50, 50, 50))
    # quad fills essentially the whole image -> no room for a neighborhood ring.
    quad = np.array([[0, 0], [199, 0], [199, 199], [0, 199]], dtype=float)

    gain, wb_r, wb_b = light_stats(template_img, quad, cfg)
    assert (gain, wb_r, wb_b) == (1.0, 1.0, 1.0)


def test_apply_mat_adds_border_sized_to_short_edge():
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    mat_color = [246, 246, 244]
    out = apply_mat(img, mat_frac=0.1, mat_color=mat_color)
    border = int(round(0.1 * 100))
    assert out.shape == (100 + 2 * border, 200 + 2 * border, 3)
    assert out[0, 0].tolist() == mat_color


def test_draw_frame_border_adds_bevel_highlight():
    img = np.zeros((50, 50, 3), dtype=np.uint8)
    out = draw_frame_border(img, frame_px=10, frame_color=[24, 22, 20], bevel=True)
    assert out.shape == (70, 70, 3)
    assert out[0, 0].tolist() != [24, 22, 20]  # bevel highlight is brighter
    assert out[69, 69].tolist() == [24, 22, 20]  # bottom-right untouched by bevel


def test_finalize_resizes_long_edge_and_returns_rgb():
    cfg = make_cfg()
    img = np.zeros((600, 900, 3), dtype=np.uint8)
    pil = finalize(img, cfg)
    assert pil.mode == "RGB"
    assert max(pil.size) == cfg.render.output_long_edge_px
    assert pil.size[0] / pil.size[1] == pytest.approx(900 / 600)


def test_warp_places_checkerboard_and_centroid_differs_from_template():
    cfg = make_cfg(mat_fraction=0.0, light_match_enabled=False)
    board = checkerboard(size=400, cell=50)  # TL cell white
    template_img = flat_template(size=1000, color=(128, 128, 128))
    # Square region sized exactly to the default square print width (20in)
    # at 20ppi -> no scaling, quad == region quad, warp ~= identity+offset.
    region = make_region([[300, 300], [700, 300], [700, 700], [300, 700]], width_inches=20)

    composed = composite_print(board, template_img, region, cfg, mat=False, frame_inches=None)

    tl_sample = composed[305, 305]
    assert tl_sample[0] > 200  # matches board's white TL cell

    # Sampled well inside a single checkerboard cell (not on the board's own
    # center vertex, where 2 white + 2 black cells meet and bilinear
    # interpolation would coincidentally average back to ~128 == gray).
    inner_sample = composed[480, 480]
    assert not np.allclose(inner_sample, (128, 128, 128), atol=10)

    outside_corner = composed[2, 2]
    assert np.abs(outside_corner.astype(int) - 128).sum() <= 6


def test_prep_master_downscales_long_edge(tmp_path):
    img = PILImage.new("RGB", (4000, 2000), (10, 20, 30))
    path = tmp_path / "master.jpg"
    img.save(path)

    arr = prep_master(path, max_long_edge=1000)
    h, w = arr.shape[:2]
    assert max(h, w) <= 1000
    assert arr.dtype == np.uint8
    assert arr.shape[2] == 3


def test_prep_master_converts_16bit_mode_without_raising(tmp_path):
    img = PILImage.new("I;16", (200, 150), 30000)
    path = tmp_path / "master16.tif"
    img.save(path)

    arr = prep_master(path, max_long_edge=1000)
    assert arr.dtype == np.uint8
    assert arr.shape == (150, 200, 3)
