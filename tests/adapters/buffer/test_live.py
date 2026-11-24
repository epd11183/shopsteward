import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from shopsteward.adapters.buffer.interface import BufferError, BufferUnknownOutcome
from shopsteward.adapters.buffer.live import LiveBufferAdapter
from shopsteward.adapters.buffer.models import BufferPostSpec
from shopsteward.pipeline.live_gate import live_buffer_error, live_buffer_open

FIXTURES = Path(__file__).parents[2] / "fixtures" / "buffer"
URL = "https://api.buffer.com/graphql"
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
SPEC = BufferPostSpec(
    channel_id="fx-channel-1", text="fx new", image_url="https://example.test/i.jpg"
)


def _fx(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _adapter(sleeps: list[float] | None = None) -> LiveBufferAdapter:
    record = sleeps if sleeps is not None else []
    return LiveBufferAdapter("fx-key", sleep=record.append)


def _route(*responses: httpx.Response) -> respx.Route:
    return respx.post(URL).mock(side_effect=list(responses))


def _ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=_fx(name))


@respx.mock
def test_list_channels_and_posts_parse_and_cache_org() -> None:
    route = _route(_ok("account"), _ok("channels"), _ok("posts"))
    a = _adapter()
    chans = a.list_channels()
    assert [c.service for c in chans] == ["pinterest", "instagram"]
    assert chans[0].boards == {"fx-board-name-1": "fx-board-1"}
    assert chans[1].is_disconnected and chans[1].is_queue_paused
    posts = a.list_posts(channel_ids=["fx-channel-1"], created_since=SINCE, with_metrics=True)
    assert route.call_count == 3  # org id fetched once
    assert route.calls.last.request.headers["authorization"] == "Bearer fx-key"
    assert "metrics { name value }" in json.loads(route.calls.last.request.content)["query"]
    sent, queued = posts
    assert sent.status == "sent" and sent.sent_at is not None
    assert sent.metrics == {"impressions": 12.0}  # null value absent, never 0
    assert sent.pin_url and queued.metrics == {} and queued.pin_url is None


@respx.mock
def test_list_posts_without_metrics_omits_selection() -> None:
    route = _route(_ok("account"), _ok("posts"))
    _adapter().list_posts(channel_ids=["x"], created_since=SINCE)
    assert "metrics {" not in json.loads(route.calls.last.request.content)["query"]


@respx.mock
def test_create_and_delete_round_trip() -> None:
    route = _route(_ok("create_post"), _ok("delete_post"))
    a = _adapter()
    post = a.create_post(SPEC)
    assert post.post_id == "fx-post-3" and post.status == "scheduled"
    inp = json.loads(route.calls[0].request.content)["variables"]["input"]
    assert inp["assets"] == [{"image": {"url": "https://example.test/i.jpg"}}]
    assert inp["aiAssisted"] is True and "metadata" not in inp
    a.delete_post("fx-post-3")


@respx.mock
def test_graphql_errors_array_maps_to_code() -> None:
    _route(httpx.Response(200, json=_fx("errors")))
    with pytest.raises(BufferError) as exc:
        _adapter().delete_post("fx-post-3")
    assert exc.value.code == "UNAUTHORIZED"


@respx.mock
def test_mutation_error_union_maps_to_mutation() -> None:
    _route(_ok("mutation_error"))
    with pytest.raises(BufferError) as exc:
        _adapter().create_post(SPEC)
    assert exc.value.code == "MUTATION" and not isinstance(exc.value, BufferUnknownOutcome)


@respx.mock
def test_long_retry_after_raises_at_once_without_sleeping() -> None:
    sleeps: list[float] = []
    route = _route(httpx.Response(429, headers={"Retry-After": "2592000"}, json={}))
    with pytest.raises(BufferError) as exc:
        _adapter(sleeps).list_channels()
    assert exc.value.code == "RATE_LIMITED" and exc.value.retry_after_s == 2592000
    assert sleeps == [] and route.call_count == 1


@respx.mock
def test_short_retry_after_sleeps_and_retries_once() -> None:
    sleeps: list[float] = []
    _route(httpx.Response(429, headers={"Retry-After": "2"}, json={}), _ok("create_post"))
    assert _adapter(sleeps).create_post(SPEC).post_id == "fx-post-3"
    assert sleeps == [2]


@respx.mock
def test_create_timeout_and_5xx_raise_unknown_outcome() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(BufferUnknownOutcome):
        _adapter().create_post(SPEC)
    respx.post(URL).mock(return_value=httpx.Response(502))
    with pytest.raises(BufferUnknownOutcome):
        _adapter().delete_post("fx-post-3")


@respx.mock
def test_read_timeout_is_plain_transport_error() -> None:
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(BufferError) as exc:
        _adapter().list_channels()
    assert exc.value.code == "TRANSPORT" and not isinstance(exc.value, BufferUnknownOutcome)


@respx.mock
def test_http_401_maps_to_unauthorized() -> None:
    _route(httpx.Response(401, json={}))
    with pytest.raises(BufferError) as exc:
        _adapter().list_channels()
    assert exc.value.code == "UNAUTHORIZED"


def test_live_buffer_gate_needs_flag_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_BUFFER", raising=False)
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    assert live_buffer_open() is False
    monkeypatch.setenv("SHOPSTEWARD_LIVE_BUFFER", "1")
    assert live_buffer_open() is False
    monkeypatch.setenv("BUFFER_API_KEY", "fx-key")
    assert live_buffer_open() is True
    monkeypatch.delenv("SHOPSTEWARD_LIVE_BUFFER")
    assert live_buffer_open() is False
    assert "fx-key" not in live_buffer_error()


# Scrub guard (design §8): fixtures must hold no real, ID-shaped values.
_BANNED = [
    re.compile(r"\b[0-9a-f]{24}\b", re.I),
    re.compile(r"buffer\.com/", re.I),
    re.compile(r"instagram\.com/", re.I),
    re.compile(r"etsystatic", re.I),
    re.compile(r"pinterest\.com/", re.I),
    re.compile(r"\d{12,}"),
]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.name)
def test_fixtures_hold_no_id_shaped_values(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for pat in _BANNED:
        assert not pat.search(text), f"{path.name}: matches {pat.pattern}"
    bad = [v for v in _id_values(json.loads(text)) if not v.startswith("fx-")]
    assert not bad, f"{path.name}: non-fx id values {bad}"


def _id_values(node: object, key: str = "") -> list[str]:
    """Every string under a key named `id` or ending in `Id`/`Ids`."""
    if isinstance(node, dict):
        return [v for k, child in node.items() for v in _id_values(child, k)]
    if isinstance(node, list):
        return [v for child in node for v in _id_values(child, key)]
    is_id_key = key == "id" or key.endswith(("Id", "Ids"))
    return [node] if is_id_key and isinstance(node, str) else []


def test_scrub_guard_catches_unmapped_id_keys() -> None:
    assert _id_values({"a": {"boardServiceId": "123"}, "tagIds": ["fx-1", "x"]}) == [
        "123",
        "fx-1",
        "x",
    ]


@respx.mock
def test_unmapped_graphql_error_on_mutation_is_unknown_outcome() -> None:
    body = {"errors": [{"message": "boom", "extensions": {"code": "INTERNAL_SERVER_ERROR"}}]}
    respx.post(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(BufferUnknownOutcome):
        _adapter().create_post(SPEC)
    with pytest.raises(BufferError) as exc:  # a read is never "unknown outcome"
        _adapter().list_channels()
    assert not isinstance(exc.value, BufferUnknownOutcome)


@respx.mock
def test_list_channels_skips_unsupported_services() -> None:
    chans = _fx("channels")
    chans["data"]["channels"].append(
        {"id": "fx-channel-3", "service": "tiktok", "name": "fx-name-3", "metadata": None}
    )
    _route(_ok("account"), httpx.Response(200, json=chans))
    assert [c.channel_id for c in _adapter().list_channels()] == ["fx-channel-1", "fx-channel-2"]


@respx.mock
def test_recorded_live_responses_parse() -> None:
    """Scrubbed real responses (recorded 2026-11-23, read-only): 3 channels incl.
    a 2-board Pinterest, sent + scheduled posts, Free-plan metrics readable."""
    _route(_ok("recorded_account"), _ok("recorded_channels"), _ok("recorded_posts"))
    a = _adapter()
    chans = a.list_channels()
    assert sorted(c.service for c in chans) == ["facebook", "instagram", "pinterest"]
    assert len(next(c for c in chans if c.service == "pinterest").boards) == 2
    assert not any(c.is_disconnected or c.is_locked for c in chans)
    posts = a.list_posts(
        channel_ids=[c.channel_id for c in chans], created_since=SINCE, with_metrics=True
    )
    assert {p.status for p in posts} == {"sent", "scheduled"}
    keys = {k for p in posts for k in p.metrics}
    assert {"impressions", "pin_clicks", "eng_rate", "saves"} <= keys
    assert all(re.fullmatch(r"[a-z0-9_]+", k) for k in keys)
