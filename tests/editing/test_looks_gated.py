import pytest

from shopsteward.adapters.look.fake import FakeLookAdapter
from shopsteward.adapters.look.interface import LookProfile, LookResult, LookUsage
from shopsteward.core.db import connect, migrate
from shopsteward.editing import looks
from shopsteward.editing.config import LOOKS_DIR
from shopsteward.editing.look_cost import month_look_cost

USER = 1
GUARD = {"max_saturation_load": 220, "max_contrast_tone": 140,
         "max_presence_load": 200, "max_split_saturation": 60}


def _conn():
    c = connect(":memory:")
    migrate(c)
    looks.seed(c, USER, LOOKS_DIR)  # for the fallback seed
    return c


def _garish():
    return LookResult(profile=LookProfile(name="g", vibrance=100, saturation=100,
        hsl={"SaturationAdjustmentOrange": 100, "SaturationAdjustmentBlue": 100}),
        usage=LookUsage(model="m", est_cost_usd=0.02))


def _tasteful():
    return LookResult(profile=LookProfile(name="t", contrast=15, vibrance=10),
                      usage=LookUsage(model="m", est_cost_usd=0.02))


def test_garish_generation_falls_back_to_seed_after_retry():
    c = _conn()
    adapter = FakeLookAdapter([_garish(), _garish()])
    out = looks.resolve_look(c, USER, "loud look", adapter, model="m", regenerate=False,
                             guard_knobs=GUARD, fallback_look="bright-and-true",
                             month_prefix="2026-08")
    assert out.name == "bright-and-true"
    assert len(adapter.calls) == 2


def test_tasteful_generation_is_kept_and_ledgered():
    c = _conn()
    adapter = FakeLookAdapter([_tasteful()])
    out = looks.resolve_look(c, USER, "nice look", adapter, model="m", regenerate=False,
                             guard_knobs=GUARD, month_prefix="2026-08", pricing={})
    assert out.contrast == 15
    assert month_look_cost(c, USER, "2026-08") == 0.02


def test_soft_cap_refuses_before_generating():
    c = _conn()
    from shopsteward.adapters.look.interface import LookUsage as U
    from shopsteward.editing.look_cost import append_llm_call
    append_llm_call(c, USER, U(model="m", est_cost_usd=5.0), description="prior")
    adapter = FakeLookAdapter([_tasteful()])
    with pytest.raises(looks.LookCostCapError):
        looks.resolve_look(c, USER, "new look", adapter, model="m", regenerate=False,
                           guard_knobs=GUARD, month_prefix="2026-08", soft_cap_usd=5.0)
    assert adapter.calls == []
