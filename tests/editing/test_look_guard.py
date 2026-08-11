from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.look_guard import sanitize_look

KNOBS = {
    "max_saturation_load": 220,
    "max_contrast_tone": 140,
    "max_presence_load": 200,
    "max_split_saturation": 60,
}


def test_tasteful_look_passes():
    lp = LookProfile(
        name="x",
        contrast=18,
        vibrance=14,
        saturation=-4,
        tone_curve=[[0, 6], [128, 128], [255, 250]],
    )
    assert sanitize_look(lp, KNOBS).ok is True


def test_oversaturated_look_rejected():
    lp = LookProfile(
        name="x",
        vibrance=100,
        saturation=100,
        hsl={"SaturationAdjustmentOrange": 100, "SaturationAdjustmentBlue": 100},
    )
    v = sanitize_look(lp, KNOBS)
    assert v.ok is False and "saturation" in v.reason.lower()


def test_extreme_presence_rejected():
    lp = LookProfile(name="x", clarity=100, dehaze=100, texture=100, whites=50, blacks=-50)
    assert sanitize_look(lp, KNOBS).ok is False


def test_harsh_split_tone_rejected():
    lp = LookProfile(name="x", split_toning={"SplitToningShadowSaturation": 90})
    assert sanitize_look(lp, KNOBS).ok is False
