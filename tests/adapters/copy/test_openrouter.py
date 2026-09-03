import json

import httpx
import respx

from shopsteward.adapters.copy.interface import CopyInputs, CopyParseError
from shopsteward.adapters.copy.openrouter import BASE, OpenRouterCopyAdapter

MODEL = "anthropic/claude-sonnet-5"
PRICING = {"anthropic/claude-sonnet-5": {"in": 2.00, "out": 10.00}}

TEMPLATE = (
    "style={house_style} subject={subject} room={strongest_room_style} "
    "risk={one_risk} rationale={rationale} orientation={orientation} "
    "format={format} sizes={sizes} formats={formats}"
)

INPUTS = CopyInputs(
    house_style="Confident field naturalist voice.",
    subject="osprey",
    strongest_room_style="coastal",
    one_risk="glare",
    rationale="strong composition",
    orientation="landscape",
    sizes=["4x5", "5x7"],
    formats=["JPEG 300 DPI", "sRGB"],
)

VALID_VERDICT = {
    "title": "Osprey in Flight Wall Art, Wildlife Decor, Coastal Style (Digital Download)",
    "tags": [f"tag{i}" for i in range(13)],
    "description": "An osprey banks over open water, talons bared for the strike.",
    "materials": None,
}


def _response(content: str, usage: dict | None = None) -> httpx.Response:
    payload: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


@respx.mock
def test_generate_copy_parses_verdict_and_usage() -> None:
    route = respx.post(BASE).mock(
        return_value=_response(
            json.dumps(VALID_VERDICT),
            usage={"prompt_tokens": 800, "completion_tokens": 300},
        )
    )
    adapter = OpenRouterCopyAdapter(
        api_key="secret-key", prompt_template=TEMPLATE, pricing=PRICING, temperature=0.4
    )

    result = adapter.generate_copy(INPUTS, model=MODEL)

    assert result.verdict.title == VALID_VERDICT["title"]
    assert result.verdict.tags == VALID_VERDICT["tags"]
    assert result.verdict.description == VALID_VERDICT["description"]
    assert result.usage is not None
    assert result.usage.model == MODEL
    assert result.usage.input_tokens == 800
    assert result.usage.output_tokens == 300
    assert result.usage.est_cost_usd == (800 / 1e6) * 2.00 + (300 / 1e6) * 10.00

    sent = route.calls.last.request
    assert sent.url == BASE
    assert sent.headers["Authorization"] == "Bearer secret-key"
    assert sent.headers["HTTP-Referer"] == "https://github.com/epd11183/shopsteward"
    assert sent.headers["X-Title"] == "ShopSteward"

    body = json.loads(sent.content)
    assert body["model"] == MODEL
    assert body["temperature"] == 0.4
    assert body["messages"][0]["role"] == "user"
    prompt_text = body["messages"][0]["content"]
    assert "style=Confident field naturalist voice." in prompt_text
    assert "subject=osprey" in prompt_text
    assert "room=coastal" in prompt_text
    assert "risk=glare" in prompt_text
    assert "rationale=strong composition" in prompt_text
    assert "orientation=landscape" in prompt_text
    assert "format=digital_download" in prompt_text
    assert "sizes=4x5, 5x7" in prompt_text
    assert "formats=JPEG 300 DPI, sRGB" in prompt_text

    schema = body["response_format"]["json_schema"]
    assert schema["name"] == "listing_copy"
    assert schema["strict"] is True
    inner = schema["schema"]
    assert inner["type"] == "object"
    assert inner["additionalProperties"] is False
    assert set(inner["required"]) == {"title", "tags", "description", "materials"}
    assert inner["properties"]["title"]["maxLength"] == 140
    assert inner["properties"]["tags"]["maxItems"] == 13
    assert inner["properties"]["tags"]["items"]["maxLength"] == 20


@respx.mock
def test_missing_field_in_response_raises_copy_parse_error() -> None:
    bad_verdict = {k: v for k, v in VALID_VERDICT.items() if k != "description"}
    respx.post(BASE).mock(return_value=_response(json.dumps(bad_verdict)))
    adapter = OpenRouterCopyAdapter(api_key="k", prompt_template=TEMPLATE)

    try:
        adapter.generate_copy(INPUTS, model=MODEL)
        raise AssertionError("expected CopyParseError")
    except CopyParseError:
        pass


@respx.mock
def test_too_many_tags_raises_copy_parse_error() -> None:
    bad_verdict = {**VALID_VERDICT, "tags": [f"tag{i}" for i in range(14)]}
    respx.post(BASE).mock(return_value=_response(json.dumps(bad_verdict)))
    adapter = OpenRouterCopyAdapter(api_key="k", prompt_template=TEMPLATE)

    try:
        adapter.generate_copy(INPUTS, model=MODEL)
        raise AssertionError("expected CopyParseError")
    except CopyParseError:
        pass


@respx.mock
def test_tag_too_long_raises_copy_parse_error() -> None:
    bad_verdict = {
        **VALID_VERDICT,
        "tags": ["a-tag-that-is-way-too-long-for-etsy", *[f"t{i}" for i in range(12)]],
    }
    respx.post(BASE).mock(return_value=_response(json.dumps(bad_verdict)))
    adapter = OpenRouterCopyAdapter(api_key="k", prompt_template=TEMPLATE)

    try:
        adapter.generate_copy(INPUTS, model=MODEL)
        raise AssertionError("expected CopyParseError")
    except CopyParseError:
        pass


@respx.mock
def test_invalid_json_content_raises_copy_parse_error() -> None:
    respx.post(BASE).mock(return_value=_response("not-json{{{"))
    adapter = OpenRouterCopyAdapter(api_key="k", prompt_template=TEMPLATE)

    try:
        adapter.generate_copy(INPUTS, model=MODEL)
        raise AssertionError("expected CopyParseError")
    except CopyParseError:
        pass


@respx.mock
def test_missing_usage_yields_none_tokens_and_cost() -> None:
    respx.post(BASE).mock(return_value=_response(json.dumps(VALID_VERDICT)))
    adapter = OpenRouterCopyAdapter(api_key="k", prompt_template=TEMPLATE, pricing=PRICING)

    result = adapter.generate_copy(INPUTS, model=MODEL)

    assert result.usage is not None
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.est_cost_usd is None


@respx.mock
def test_huge_payload_error_message_is_truncated() -> None:
    huge_choices = [{"message": {"content": "x" * 5000}}]
    respx.post(BASE).mock(return_value=httpx.Response(200, json={"choices": huge_choices}))
    adapter = OpenRouterCopyAdapter(api_key="k", prompt_template=TEMPLATE)

    try:
        adapter.generate_copy(INPUTS, model=MODEL)
        raise AssertionError("expected CopyParseError")
    except CopyParseError as exc:
        assert len(str(exc)) < 700


@respx.mock
def test_truncated_http_body_raises_copy_parse_error_not_bare_json_error() -> None:
    """A truncated HTTP response body (not just truncated model content --
    e.g. a proxy/edge cutting the connection mid-response) used to raise a
    bare json.JSONDecodeError from resp.json(), which sat OUTSIDE the
    adapter's try block -- the caller's narrow `except CopyParseError`
    (pipeline/listings/copy.py) wouldn't catch that, crashing the whole
    pipeline the same way the original bug did. resp.json() now runs inside
    the same try, so this failure mode surfaces as CopyParseError too."""
    respx.post(BASE).mock(
        return_value=httpx.Response(
            200, content=b'{"choices": [{"message": {"content": "truncat', headers={}
        )
    )
    adapter = OpenRouterCopyAdapter(api_key="k", prompt_template=TEMPLATE)

    try:
        adapter.generate_copy(INPUTS, model=MODEL)
        raise AssertionError("expected CopyParseError")
    except CopyParseError:
        pass
