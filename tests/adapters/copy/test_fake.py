import pytest
from pydantic import ValidationError

from shopsteward.adapters.copy.fake import FakeCopyAdapter, FixtureCopyAdapter
from shopsteward.adapters.copy.interface import CopyInputs, CopyParseError, CopyResult, CopyVerdict

INPUTS = CopyInputs(
    house_style="Confident field naturalist voice.",
    subject="osprey",
    strongest_room_style="coastal",
    orientation="landscape",
    sizes=["4x5"],
    formats=["JPEG 300 DPI"],
)


def test_fixture_adapter_is_deterministic():
    adapter = FixtureCopyAdapter()
    first = adapter.generate_copy(INPUTS, model="any-model")
    second = adapter.generate_copy(INPUTS, model="any-model")
    assert first.verdict == second.verdict
    assert first.usage is None


def test_fixture_adapter_verdict_is_schema_valid():
    result = FixtureCopyAdapter().generate_copy(INPUTS, model="any-model")
    assert len(result.verdict.title) <= 140
    assert len(result.verdict.tags) == 13
    assert all(len(tag) <= 20 for tag in result.verdict.tags)


def test_fixture_adapter_varies_with_inputs():
    other = CopyInputs(
        **{**INPUTS.model_dump(), "subject": "bear", "strongest_room_style": "cabin"}
    )
    result_a = FixtureCopyAdapter().generate_copy(INPUTS, model="any-model")
    result_b = FixtureCopyAdapter().generate_copy(other, model="any-model")
    assert result_a.verdict.title != result_b.verdict.title


def test_fake_adapter_replays_queued_results_in_order():
    verdict = CopyVerdict(title="t", tags=["a"] * 13, description="d")
    queued = CopyResult(verdict=verdict, usage=None)
    adapter = FakeCopyAdapter([queued])

    result = adapter.generate_copy(INPUTS, model="m")
    assert result is queued
    assert adapter.calls == [(INPUTS, "m")]


def test_fake_adapter_raises_queued_exception():
    adapter = FakeCopyAdapter([CopyParseError("boom")])
    with pytest.raises(CopyParseError):
        adapter.generate_copy(INPUTS, model="m")


def test_fake_adapter_exhausted_raises():
    adapter = FakeCopyAdapter([])
    with pytest.raises(RuntimeError):
        adapter.generate_copy(INPUTS, model="m")


def test_copy_verdict_rejects_too_many_tags():
    with pytest.raises(ValidationError):
        CopyVerdict(title="t", tags=["a"] * 14, description="d")


def test_copy_verdict_rejects_tag_too_long():
    with pytest.raises(ValidationError):
        CopyVerdict(title="t", tags=["a" * 21] + ["b"] * 12, description="d")


def test_copy_verdict_rejects_title_too_long():
    with pytest.raises(ValidationError):
        CopyVerdict(title="t" * 141, tags=["a"] * 13, description="d")


def test_copy_verdict_requires_description():
    with pytest.raises(ValidationError):
        CopyVerdict(title="t", tags=["a"] * 13)


def test_copy_verdict_rejects_tag_containing_comma():
    # Etsy's write path comma-joins tags into a single form field; a tag
    # containing a literal comma would silently split into extra tags on
    # the wire (undetectable server-side -- each fragment is individually
    # valid). Reject at the source instead.
    with pytest.raises(ValidationError):
        CopyVerdict(title="t", tags=["black, white", "red"], description="d")
