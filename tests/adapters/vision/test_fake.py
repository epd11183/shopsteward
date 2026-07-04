import pytest

from shopsteward.adapters.vision.fake import FakeVisionAdapter, FixtureVisionAdapter
from shopsteward.adapters.vision.interface import VisionResult, VisionUsage, VisionVerdict

MODEL = "gemini-2.5-flash"


def _verdict(score: int) -> VisionVerdict:
    return VisionVerdict(
        commercial_score=score,
        subject="s",
        strongest_room_style="neutral",
        one_risk="r",
        rationale="rationale",
    )


def test_fixture_adapter_is_deterministic_for_same_bytes() -> None:
    adapter = FixtureVisionAdapter()
    result_a = adapter.score_commercial(b"photo-one", model=MODEL)
    result_b = adapter.score_commercial(b"photo-one", model=MODEL)
    assert result_a.verdict == result_b.verdict
    assert result_a.usage is None


def test_fixture_adapter_differs_for_different_bytes() -> None:
    adapter = FixtureVisionAdapter()
    result_a = adapter.score_commercial(b"photo-one", model=MODEL)
    result_b = adapter.score_commercial(b"photo-two", model=MODEL)
    assert result_a.verdict.commercial_score != result_b.verdict.commercial_score


def test_fixture_adapter_score_within_bounds() -> None:
    adapter = FixtureVisionAdapter()
    for payload in (b"a", b"bb", b"ccc", b"dddd", b"eeeee"):
        result = adapter.score_commercial(payload, model=MODEL)
        assert 30 <= result.verdict.commercial_score <= 90


def test_fake_adapter_pops_results_in_order() -> None:
    first = VisionResult(verdict=_verdict(40), usage=None)
    second = VisionResult(
        verdict=_verdict(80), usage=VisionUsage(model=MODEL, input_tokens=10, output_tokens=5)
    )
    adapter = FakeVisionAdapter([first, second])

    assert adapter.score_commercial(b"one-two-three", model=MODEL) is first
    assert adapter.score_commercial(b"four-five", model=MODEL) is second
    assert adapter.calls == [(len(b"one-two-three"), MODEL), (len(b"four-five"), MODEL)]


def test_fake_adapter_raises_queued_exception() -> None:
    adapter = FakeVisionAdapter([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        adapter.score_commercial(b"x", model=MODEL)


def test_fake_adapter_raises_when_exhausted() -> None:
    adapter = FakeVisionAdapter([])
    with pytest.raises(RuntimeError, match="exhausted"):
        adapter.score_commercial(b"x", model=MODEL)
