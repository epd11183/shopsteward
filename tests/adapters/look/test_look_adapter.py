import httpx
import pytest
import respx

from shopsteward.adapters.look.fake import FakeLookAdapter, FixtureLookAdapter
from shopsteward.adapters.look.interface import LookParseError, LookProfile, LookResult
from shopsteward.adapters.look.openrouter import BASE, OpenRouterLookAdapter


def test_fixture_adapter_is_deterministic():
    a = FixtureLookAdapter().generate_look("cinematic mexico", model="m")
    b = FixtureLookAdapter().generate_look("cinematic mexico", model="m")
    assert a.profile.model_dump() == b.profile.model_dump()
    assert a.usage is None


def test_fake_adapter_replays_queue():
    queued = LookResult(profile=LookProfile(name="q", contrast=5))
    fake = FakeLookAdapter([queued])
    assert fake.generate_look("x", model="m").profile.contrast == 5
    with pytest.raises(RuntimeError):
        fake.generate_look("x", model="m")


@respx.mock
def test_openrouter_parses_profile():
    content = (
        '{"contrast": 12, "tone_curve": [[0,0],[255,255]], "hsl": {}, '
        '"split_toning": {}, "vibrance": 8, "saturation": 0}'
    )
    payload = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    respx.post(BASE).mock(return_value=httpx.Response(200, json=payload))
    adapter = OpenRouterLookAdapter(api_key="k", prompt_template="{description}")
    result = adapter.generate_look("warm and moody", model="test/model")
    assert result.profile.contrast == 12
    assert result.profile.name == "warm and moody"
    assert result.usage.output_tokens == 20


@respx.mock
def test_openrouter_raises_on_bad_json():
    payload = {"choices": [{"message": {"content": "not json"}}]}
    respx.post(BASE).mock(return_value=httpx.Response(200, json=payload))
    adapter = OpenRouterLookAdapter(api_key="k", prompt_template="{description}")
    with pytest.raises(LookParseError):
        adapter.generate_look("x", model="m")
