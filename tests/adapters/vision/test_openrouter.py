import base64
import json

import httpx
import respx

from shopsteward.adapters.vision.interface import VisionParseError
from shopsteward.adapters.vision.openrouter import BASE, OpenRouterVisionAdapter

MODEL = "google/gemini-2.5-flash-lite"
PROMPT = "Score this room photo for commercial viability."
JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

PRICING = {"google/gemini-2.5-flash-lite": {"in": 0.10, "out": 0.40}}

VALID_VERDICT = {
    "commercial_score": 72,
    "subject": "coastal living room",
    "strongest_room_style": "coastal",
    "one_risk": "glare on glass table",
    "rationale": "Bright, well-composed, sells well in coastal-themed listings.",
}


def _response(content: str, usage: dict | None = None) -> httpx.Response:
    payload: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


@respx.mock
def test_score_commercial_parses_verdict_and_usage() -> None:
    route = respx.post(BASE).mock(
        return_value=_response(
            json.dumps(VALID_VERDICT),
            usage={"prompt_tokens": 1000, "completion_tokens": 200},
        )
    )
    adapter = OpenRouterVisionAdapter(api_key="secret-key", prompt=PROMPT, pricing=PRICING)

    result = adapter.score_commercial(JPEG_BYTES, model=MODEL)

    assert result.verdict.commercial_score == 72
    assert result.verdict.subject == "coastal living room"
    assert result.verdict.strongest_room_style == "coastal"
    assert result.verdict.one_risk == "glare on glass table"
    assert result.usage is not None
    assert result.usage.model == MODEL
    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 200
    assert result.usage.est_cost_usd == (1000 / 1e6) * 0.10 + (200 / 1e6) * 0.40

    sent = route.calls.last.request
    assert sent.url == BASE
    assert sent.headers["Authorization"] == "Bearer secret-key"
    assert sent.headers["HTTP-Referer"] == "https://github.com/epd11183/shopsteward"
    assert sent.headers["X-Title"] == "ShopSteward"

    body = json.loads(sent.content)
    assert body["model"] == MODEL
    assert body["temperature"] == 0

    content = body["messages"][0]["content"]
    image_url = content[0]["image_url"]["url"]
    assert image_url == ("data:image/jpeg;base64," + base64.b64encode(JPEG_BYTES).decode("ascii"))
    assert content[1]["text"] == PROMPT

    schema = body["response_format"]["json_schema"]
    assert schema["name"] == "vision_verdict"
    assert schema["strict"] is True
    inner = schema["schema"]
    assert inner["type"] == "object"
    assert inner["additionalProperties"] is False
    assert set(inner["required"]) == {
        "commercial_score",
        "subject",
        "strongest_room_style",
        "one_risk",
        "rationale",
    }


@respx.mock
def test_invalid_json_content_raises_vision_parse_error() -> None:
    respx.post(BASE).mock(return_value=_response("not-json{{{"))
    adapter = OpenRouterVisionAdapter(api_key="k", prompt=PROMPT)

    try:
        adapter.score_commercial(JPEG_BYTES, model=MODEL)
        raise AssertionError("expected VisionParseError")
    except VisionParseError:
        pass


@respx.mock
def test_missing_choices_raises_vision_parse_error() -> None:
    respx.post(BASE).mock(return_value=httpx.Response(200, json={}))
    adapter = OpenRouterVisionAdapter(api_key="k", prompt=PROMPT)

    try:
        adapter.score_commercial(JPEG_BYTES, model=MODEL)
        raise AssertionError("expected VisionParseError")
    except VisionParseError:
        pass


@respx.mock
def test_verdict_out_of_range_raises_vision_parse_error() -> None:
    bad_verdict = {**VALID_VERDICT, "commercial_score": 150}
    respx.post(BASE).mock(return_value=_response(json.dumps(bad_verdict)))
    adapter = OpenRouterVisionAdapter(api_key="k", prompt=PROMPT)

    try:
        adapter.score_commercial(JPEG_BYTES, model=MODEL)
        raise AssertionError("expected VisionParseError")
    except VisionParseError:
        pass


@respx.mock
def test_missing_usage_yields_none_tokens_and_cost() -> None:
    respx.post(BASE).mock(return_value=_response(json.dumps(VALID_VERDICT)))
    adapter = OpenRouterVisionAdapter(api_key="k", prompt=PROMPT, pricing=PRICING)

    result = adapter.score_commercial(JPEG_BYTES, model=MODEL)

    assert result.usage is not None
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.est_cost_usd is None


@respx.mock
def test_huge_payload_error_message_is_truncated() -> None:
    huge_choices = [{"message": {"content": "x" * 5000}}]
    respx.post(BASE).mock(return_value=httpx.Response(200, json={"choices": huge_choices}))
    adapter = OpenRouterVisionAdapter(api_key="k", prompt=PROMPT)

    try:
        adapter.score_commercial(JPEG_BYTES, model=MODEL)
        raise AssertionError("expected VisionParseError")
    except VisionParseError as exc:
        assert len(str(exc)) < 700
