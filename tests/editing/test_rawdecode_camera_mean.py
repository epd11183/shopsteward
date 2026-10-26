from types import SimpleNamespace

import numpy as np

from shopsteward.editing.rawdecode import _camera_mean


def _raw(pattern, plane_values, black=(100, 100, 100, 100), white=16000, size=40):
    """Stub rawpy object: an RGGB-style mosaic with constant per-site values."""
    pattern = np.array(pattern)
    ph, pw = pattern.shape
    cols = np.tile(pattern, (size // ph, size // pw))
    img = np.zeros(cols.shape, dtype=np.uint16)
    for c, v in plane_values.items():
        img[cols == c] = v
    return SimpleNamespace(
        raw_pattern=pattern,
        raw_colors_visible=cols,
        raw_image_visible=img,
        black_level_per_channel=list(black),
        white_level=white,
    )


def test_camera_mean_black_subtracts_and_averages_greens():
    rng = np.random.default_rng(0)
    raw = _raw([[0, 1], [3, 2]], {0: 1100, 1: 2100, 3: 2100, 2: 600})
    # Vary brightness so the percentile mask keeps a real mid-tone set.
    raw.raw_image_visible = (
        raw.raw_image_visible * rng.uniform(0.5, 1.5, raw.raw_image_visible.shape)
    ).astype(np.float32)
    raw.black_level_per_channel = [0, 0, 0, 0]

    r, g, b = _camera_mean(raw)

    assert 1.6 < g / r < 2.2 and 3.0 < g / b < 4.2


def test_camera_mean_none_for_xtrans_or_black_frame():
    assert _camera_mean(_raw(np.zeros((6, 6), dtype=int), {0: 500})) is None
    assert _camera_mean(_raw([[0, 1], [3, 2]], {0: 100, 1: 100, 3: 100, 2: 100})) is None
