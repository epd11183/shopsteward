import base64
import json

import httpx
import respx

from shopsteward.adapters.vision.gemini import BASE, GeminiVisionAdapter, VisionParseError

MODEL = "gemini-2.5-flash"
PROMPT = "Score this room photo for commercial viability."
JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

PRICING = {"gemini-2.5-flash": {"in": 0.30, "out": 2.50}}

VALID_VERDICT = {
    "commercial_score": 72,
    "subject": "coastal living room",
    "strongest_room_style": "coastal",
    "one_risk": "glare on glass table",
    "rationale": "Bright, well-composed, sells well in coastal-themed listings.",
}


def _response(text: str, usage_metadata: dict | None = None) -> httpx.Response:
    payload: dict = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    if usage_metadata is not None:
        payload["usageMetadata"] = usage_metadata
    return httpx.Response(200, json=payload)


@respx.mock
def test_score_commercial_parses_verdict_and_usage() -> None:
    route = respx.post(f"{BASE}/{MODEL}:generateContent").mock(
        return_value=_response(
            json.dumps(VALID_VERDICT),
            usage_metadata={"promptTokenCount": 1000, "candidatesTokenCount": 200},
        )
    )
    adapter = GeminiVisionAdapter(api_key="secret-key", prompt=PROMPT, pricing=PRICING)

    result = adapter.score_commercial(JPEG_BYTES, model=MODEL)

    assert result.verdict.commercial_score == 72
    assert result.verdict.subject == "coastal living room"
    assert result.verdict.strongest_room_style == "coastal"
    assert result.verdict.one_risk == "glare on glass table"
    assert result.usage is not None
    assert result.usage.model == MODEL
    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 200
    assert result.usage.est_cost_usd == (1000 / 1e6) * 0.30 + (200 / 1e6) * 2.50

    sent = route.calls.last.request
    assert sent.url == f"{BASE}/{MODEL}:generateContent"
    assert sent.headers["x-goog-api-key"] == "secret-key"

    body = json.loads(sent.content)
    parts = body["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg"
    assert parts[0]["inlineData"]["data"] == base64.b64encode(JPEG_BYTES).decode("ascii")
    assert parts[1]["text"] == PROMPT

    gen_config = body["generationConfig"]
    assert gen_config["temperature"] == 0
    assert gen_config["responseMimeType"] == "application/json"
    schema = gen_config["responseSchema"]
    assert schema["type"] == "OBJECT"
    assert set(schema["required"]) == {
        "commercial_score",
        "subject",
        "strongest_room_style",
        "one_risk",
        "rationale",
    }


@respx.mock
def test_invalid_json_text_raises_vision_parse_error() -> None:
    respx.post(f"{BASE}/{MODEL}:generateContent").mock(return_value=_response("not-json{{{"))
    adapter = GeminiVisionAdapter(api_key="k", prompt=PROMPT)

    try:
        adapter.score_commercial(JPEG_BYTES, model=MODEL)
        raise AssertionError("expected VisionParseError")
    except VisionParseError:
        pass


@respx.mock
def test_verdict_out_of_range_raises_vision_parse_error() -> None:
    bad_verdict = {**VALID_VERDICT, "commercial_score": 150}
    respx.post(f"{BASE}/{MODEL}:generateContent").mock(
        return_value=_response(json.dumps(bad_verdict))
    )
    adapter = GeminiVisionAdapter(api_key="k", prompt=PROMPT)

    try:
        adapter.score_commercial(JPEG_BYTES, model=MODEL)
        raise AssertionError("expected VisionParseError")
    except VisionParseError:
        pass


@respx.mock
def test_google_prefixed_model_id_is_stripped_for_url_but_not_for_pricing() -> None:
    """Tuning-profile model ids are OpenRouter-prefixed (PRD §13 decision 36)
    so the same id + pricing entry serves both providers; the gemini fallback
    must strip "google/" for the native generateContent URL while still
    keying the pricing lookup off the original, prefixed id."""
    prefixed_model = "google/gemini-2.5-pro"
    native_path = "gemini-2.5-pro"
    pricing = {prefixed_model: {"in": 1.25, "out": 10.00}}

    route = respx.post(f"{BASE}/{native_path}:generateContent").mock(
        return_value=_response(
            json.dumps(VALID_VERDICT),
            usage_metadata={"promptTokenCount": 1000, "candidatesTokenCount": 200},
        )
    )
    adapter = GeminiVisionAdapter(api_key="k", prompt=PROMPT, pricing=pricing)

    result = adapter.score_commercial(JPEG_BYTES, model=prefixed_model)

    sent = route.calls.last.request
    assert sent.url == f"{BASE}/{native_path}:generateContent"
    assert "google/" not in str(sent.url)

    assert result.usage is not None
    assert result.usage.model == prefixed_model
    assert result.usage.est_cost_usd == (1000 / 1e6) * 1.25 + (200 / 1e6) * 10.00


@respx.mock
def test_bare_model_id_behavior_is_unchanged() -> None:
    route = respx.post(f"{BASE}/{MODEL}:generateContent").mock(
        return_value=_response(
            json.dumps(VALID_VERDICT),
            usage_metadata={"promptTokenCount": 1000, "candidatesTokenCount": 200},
        )
    )
    adapter = GeminiVisionAdapter(api_key="k", prompt=PROMPT, pricing=PRICING)

    result = adapter.score_commercial(JPEG_BYTES, model=MODEL)

    assert route.calls.last.request.url == f"{BASE}/{MODEL}:generateContent"
    assert result.usage is not None
    assert result.usage.est_cost_usd == (1000 / 1e6) * 0.30 + (200 / 1e6) * 2.50


@respx.mock
def test_missing_usage_metadata_yields_none_tokens_and_cost() -> None:
    respx.post(f"{BASE}/{MODEL}:generateContent").mock(
        return_value=_response(json.dumps(VALID_VERDICT))
    )
    adapter = GeminiVisionAdapter(api_key="k", prompt=PROMPT, pricing=PRICING)

    result = adapter.score_commercial(JPEG_BYTES, model=MODEL)

    assert result.usage is not None
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.est_cost_usd is None
