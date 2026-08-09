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
    "cast_trigger": 0.06,
    "cast_nudge_cap": 8,
}


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
    assert cs.exposure <= KNOBS["exposure_max_stops"]


def test_very_dark_triggers_shadow_lift():
    cs = analyze_raw(_flat(0.05), KNOBS)
    assert cs.shadow_lift > 0
    assert cs.shadow_range_high == 45


def test_midtone_image_no_shadow_lift():
    cs = analyze_raw(_flat(0.5), KNOBS)
    assert cs.shadow_lift == 0.0


def test_green_cast_nudge_is_capped_and_recorded():
    cs = analyze_raw(_flat(0.4, cast=(0.85, 1.15, 0.85)), KNOBS)
    assert cs.tint_nudge != 0
    assert abs(cs.tint_nudge) <= KNOBS["cast_nudge_cap"]


def test_neutral_image_no_cast_nudge():
    cs = analyze_raw(_flat(0.4), KNOBS)
    assert cs.tint_nudge == 0


def test_average_corrections_means_exposure():
    a = analyze_raw(_flat(0.15), KNOBS)
    b = analyze_raw(_flat(0.75), KNOBS)
    avg = average_corrections([a, b])
    assert min(a.exposure, b.exposure) <= avg.exposure <= max(a.exposure, b.exposure)
