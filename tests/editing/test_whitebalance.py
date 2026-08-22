"""Tests for the colorimetric WB/tint estimator.

Focus: matrix-direction sanity, monotonic direction (warm/cool, green/magenta),
offset pass-through, and bounds. Exact CCT is camera/profile-approximate, so we
assert plausible bands and strict ordering, not exact values.
"""

from types import SimpleNamespace

import numpy as np

from shopsteward.editing.whitebalance import estimate_wb

# Verified Canon-ish libraw rgb_xyz_matrix (XYZ->camera); estimator inverts it.
XYZ_MATRIX = np.array(
    [
        [0.9396, -0.2598, -0.1207],
        [-0.4408, 1.2296, 0.2369],
        [-0.0505, 0.1575, 0.6077],
    ]
)

# Physics of WB multipliers: they NEUTRALIZE the raw neutral, so a warm
# (reddish) illuminant yields a SMALL red multiplier. Warmer => lower red mult.
DAYLIGHT = (1896.0, 1024.0, 1547.0, 1024.0)
WARM = (1300.0, 1024.0, 1900.0, 1024.0)  # less red / more blue mult => warmer


def _decoded(mult, rgb_fill=0.4):
    return SimpleNamespace(
        wb_multipliers=mult,
        xyz_matrix=XYZ_MATRIX,
        rgb=np.full((4, 4, 3), rgb_fill, dtype=np.float32),
    )


def test_daylight_lands_in_plausible_band():
    temp, tint = estimate_wb(_decoded(DAYLIGHT), {})
    assert 4000 <= temp <= 9000  # plausible daylight, right direction
    assert -150 <= tint <= 150


def test_warmer_illuminant_is_lower_kelvin():
    day, _ = estimate_wb(_decoded(DAYLIGHT), {})
    warm, _ = estimate_wb(_decoded(WARM), {})
    assert warm < day


def test_green_bias_moves_tint_toward_green():
    # Lower green multiplier => illuminant is green-deficient-neutralized =>
    # estimated illuminant reads green => tint toward green (more negative).
    neutral_tint = estimate_wb(_decoded(DAYLIGHT), {})[1]
    green = (1896.0, 800.0, 1547.0, 800.0)
    green_tint = estimate_wb(_decoded(green), {})[1]
    assert green_tint < neutral_tint


def test_temp_offset_raises_result():
    base, _ = estimate_wb(_decoded(DAYLIGHT), {})
    offset, _ = estimate_wb(_decoded(DAYLIGHT), {"wb_temp_offset_k": 500})
    assert abs((offset - base) - 500) <= 1  # rounding only


def test_tint_offset_shifts_tint():
    _, base = estimate_wb(_decoded(DAYLIGHT), {})
    _, shifted = estimate_wb(_decoded(DAYLIGHT), {"wb_tint_offset": 10})
    assert abs((shifted - base) - 10) <= 1


def test_clamps_stay_in_range():
    # Extreme offsets must clamp, not overflow.
    temp, tint = estimate_wb(
        _decoded(DAYLIGHT),
        {"wb_temp_offset_k": 10_000_000, "wb_tint_offset": 10_000},
    )
    assert temp == 50000
    assert tint == 150
    temp2, tint2 = estimate_wb(
        _decoded(DAYLIGHT),
        {"wb_temp_offset_k": -10_000_000, "wb_tint_offset": -10_000},
    )
    assert temp2 == 2000
    assert tint2 == -150


def test_grayworld_blend_runs_and_is_bounded():
    temp, tint = estimate_wb(_decoded(DAYLIGHT), {"grayworld_blend": 0.5})
    assert 2000 <= temp <= 50000
    assert -150 <= tint <= 150


def test_missing_xyz_matrix_falls_back_to_as_shot():
    decoded = SimpleNamespace(
        wb_multipliers=DAYLIGHT,
        xyz_matrix=None,
        rgb=np.full((4, 4, 3), 0.4, dtype=np.float32),
    )
    assert estimate_wb(decoded, {}) == (None, None)


def test_singular_xyz_matrix_falls_back_to_as_shot():
    decoded = SimpleNamespace(
        wb_multipliers=DAYLIGHT,
        xyz_matrix=np.zeros((3, 3)),
        rgb=np.full((4, 4, 3), 0.4, dtype=np.float32),
    )
    assert estimate_wb(decoded, {}) == (None, None)
