import pytest
from pydantic import ValidationError

from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.models import CorrectionSettings


def test_lookprofile_defaults():
    lp = LookProfile(name="x")
    assert lp.contrast == 0 and lp.tone_curve == [] and lp.vibrance == 0


def test_lookprofile_rejects_out_of_range_contrast():
    with pytest.raises(ValidationError):
        LookProfile(name="x", contrast=500)


def test_correctionsettings_defaults():
    cs = CorrectionSettings()
    assert cs.exposure == 0.0 and cs.shadow_lift == 0.0
    assert cs.temp_nudge == 0 and cs.white_balance == "As Shot"
