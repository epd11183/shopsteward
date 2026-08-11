import pytest

from shopsteward.adapters.planner.fake import FakePlannerAdapter
from shopsteward.adapters.planner.interface import PlannerNarration, PlannerUsage

BRIEF_TEXT = "ShopSteward -- 2026-08-11\n\nTHE SHOP\n  Revenue: $100.00"


def test_fake_adapter_default_narration_is_deterministic_and_zero_cost():
    adapter = FakePlannerAdapter()
    first = adapter.narrate(BRIEF_TEXT)
    second = adapter.narrate(BRIEF_TEXT)
    assert isinstance(first, PlannerNarration)
    assert first.text == second.text
    assert first.usage.est_cost_usd == 0.0
    assert adapter.calls == [BRIEF_TEXT, BRIEF_TEXT]


def test_fake_adapter_replays_queued_results_in_order():
    narration = PlannerNarration(
        text="queued", usage=PlannerUsage(prompt_tokens=10, completion_tokens=5, est_cost_usd=0.01)
    )
    adapter = FakePlannerAdapter([narration])

    result = adapter.narrate(BRIEF_TEXT)

    assert result is narration
    assert adapter.calls == [BRIEF_TEXT]


def test_fake_adapter_raises_queued_exception():
    adapter = FakePlannerAdapter([RuntimeError("boom")])
    with pytest.raises(RuntimeError):
        adapter.narrate(BRIEF_TEXT)


def test_fake_adapter_exhausted_raises():
    adapter = FakePlannerAdapter([])
    with pytest.raises(RuntimeError):
        adapter.narrate(BRIEF_TEXT)
