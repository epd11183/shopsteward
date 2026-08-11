import numpy as np

from shopsteward.editing.analyze import analyze_raw, average_corrections
from shopsteward.editing.rawdecode import DecodedImage

KNOBS = {
    "exposure_target_luma": 0.45,
    "exposure_max_stops": 1.5,
    "shadow_trigger_luma": 0.12,
    "shadow_lift_max": 1.0,
    "shadow_range_low": 0,
    "shadow_range_high": 45,
}

# Verified Canon-ish libraw rgb_xyz_matrix (XYZ->camera); estimator inverts it.
XYZ_MATRIX = np.array(
    [
        [0.9396, -0.2598, -0.1207],
        [-0.4408, 1.2296, 0.2369],
        [-0.0505, 0.1575, 0.6077],
    ]
)
DAYLIGHT_WB = (1896.0, 1024.0, 1547.0, 1024.0)


def _flat(value, cast=(1.0, 1.0, 1.0)):
    img = np.zeros((16, 16, 3), dtype=np.float32)
    img[:] = np.array(value, dtype=np.float32) * np.array(cast, dtype=np.float32)
    return DecodedImage(rgb=np.clip(img, 0, 1))


def test_dark_image_gets_positive_exposure():
    cs = analyze_raw(_flat(0.15), KNOBS)
    assert cs.exposure > 0


def test_bright_image_gets_negative_exposure():
    cs = analyze_raw(_flat(0.8), KNOBS)
    assert cs.exposure < 0


def test_exposure_is_capped():
    cs = analyze_raw(_flat(0.001), KNOBS)
    assert cs.exposure == KNOBS["exposure_max_stops"]


def test_blown_highlights_suppress_positive_exposure():
    # Low median (wants brightening) but a near-clipping bright region (a blown
    # sky). Highlight protection must cancel the positive push so we don't blow
    # it further; a flat-dark frame with no bright region still brightens.
    knobs = {**KNOBS, "exposure_highlight_ceiling": 0.92}
    img = np.full((16, 16, 3), 0.15, dtype=np.float32)
    img[:3, :, :] = 0.98  # ~19% of pixels near clipping (a hot sky)
    with_sky = DecodedImage(rgb=img)
    assert analyze_raw(with_sky, knobs).exposure == 0.0
    # Same dark base, no bright region -> positive exposure as before.
    assert analyze_raw(_flat(0.15), knobs).exposure > 0


def test_exposure_bias_shifts_uniformly():
    base = analyze_raw(_flat(0.3), KNOBS).exposure
    biased = analyze_raw(_flat(0.3), {**KNOBS, "exposure_bias": -0.45}).exposure
    assert round(biased - base, 2) == -0.45


def test_highlight_recovery_scales_with_clipping():
    # A frame with a large near-clipping region gets strong (negative) recovery;
    # a clean midtone frame gets none.
    img = np.full((16, 16, 3), 0.3, dtype=np.float32)
    img[:6, :, :] = 0.97  # ~37% near-clipped -> saturates recovery
    assert analyze_raw(DecodedImage(rgb=img), KNOBS).highlight_recovery <= -60
    assert analyze_raw(_flat(0.4), KNOBS).highlight_recovery == 0


def test_black_point_deepens_hazy_frames_only():
    # Lifted/hazy shadows (min ~0.12) get a negative black point; a frame with
    # true blacks (min ~0) is left alone.
    hazy = np.full((16, 16, 3), 0.12, dtype=np.float32)
    assert analyze_raw(DecodedImage(rgb=hazy), KNOBS).black_point < 0
    crushed = np.full((16, 16, 3), 0.3, dtype=np.float32)
    crushed[:2, :, :] = 0.0
    assert analyze_raw(DecodedImage(rgb=crushed), KNOBS).black_point == 0


def test_very_dark_triggers_shadow_lift():
    cs = analyze_raw(_flat(0.05), KNOBS)
    assert cs.shadow_lift > 0
    assert cs.shadow_range_high == 45


def test_midtone_image_no_shadow_lift():
    cs = analyze_raw(_flat(0.5), KNOBS)
    assert cs.shadow_lift == 0.0


def test_auto_wb_off_leaves_temperature_and_tint_none():
    cs = analyze_raw(_flat(0.4), KNOBS)  # flag absent
    assert cs.temperature is None and cs.tint is None
    cs = analyze_raw(_flat(0.4), {**KNOBS, "auto_white_balance": False})
    assert cs.temperature is None and cs.tint is None


def test_auto_wb_on_sets_temperature_and_tint():
    img = np.full((16, 16, 3), 0.4, dtype=np.float32)
    decoded = DecodedImage(rgb=img, wb_multipliers=DAYLIGHT_WB, xyz_matrix=XYZ_MATRIX)
    cs = analyze_raw(decoded, {**KNOBS, "auto_white_balance": True})
    assert isinstance(cs.temperature, int) and isinstance(cs.tint, int)
    assert 2000 <= cs.temperature <= 50000
    assert -150 <= cs.tint <= 150


def test_all_black_returns_capped_exposure():
    cs = analyze_raw(_flat(0.0), KNOBS)
    assert cs.exposure == KNOBS["exposure_max_stops"]


def test_average_corrections_means_exposure():
    a = analyze_raw(_flat(0.15), KNOBS)
    b = analyze_raw(_flat(0.75), KNOBS)
    avg = average_corrections([a, b])
    assert min(a.exposure, b.exposure) <= avg.exposure <= max(a.exposure, b.exposure)
