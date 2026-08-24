import json

import httpx
import respx

from shopsteward.adapters.planner.interface import PlannerLimits, PlannerParseError
from shopsteward.adapters.planner.openrouter import BASE, OpenRouterPlannerAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops.planner import narrate_brief

MODEL = "google/gemini-2.5-flash-lite"
PRICING = {MODEL: {"in": 0.10, "out": 0.40}}

BRIEF_TEXT = "ShopSteward -- 2026-08-11\n\nTHE SHOP\n  Revenue: $100.00"


def _response(content: str, usage: dict | None = None) -> httpx.Response:
    payload: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


@respx.mock
def test_narrate_parses_text_and_usage() -> None:
    route = respx.post(BASE).mock(
        return_value=_response(
            "Revenue is steady.", usage={"prompt_tokens": 800, "completion_tokens": 300}
        )
    )
    adapter = OpenRouterPlannerAdapter(model=MODEL, api_key="secret-key", est_cost_per_mtok=PRICING)

    result = adapter.narrate(BRIEF_TEXT)

    assert result.text == "Revenue is steady."
    assert result.usage.prompt_tokens == 800
    assert result.usage.completion_tokens == 300
    assert result.usage.est_cost_usd == (800 / 1e6) * 0.10 + (300 / 1e6) * 0.40

    sent = route.calls.last.request
    assert sent.url == BASE
    assert sent.headers["Authorization"] == "Bearer secret-key"
    body = json.loads(sent.content)
    assert body["model"] == MODEL
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == BRIEF_TEXT


@respx.mock
def test_missing_choices_raises_planner_parse_error() -> None:
    respx.post(BASE).mock(return_value=httpx.Response(200, json={}))
    adapter = OpenRouterPlannerAdapter(model=MODEL, api_key="k")

    try:
        adapter.narrate(BRIEF_TEXT)
        raise AssertionError("expected PlannerParseError")
    except PlannerParseError:
        pass


@respx.mock
def test_non_json_body_raises_planner_parse_error_not_bare_valueerror() -> None:
    respx.post(BASE).mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    adapter = OpenRouterPlannerAdapter(model=MODEL, api_key="k")

    try:
        adapter.narrate(BRIEF_TEXT)
        raise AssertionError("expected PlannerParseError")
    except PlannerParseError:
        pass


@respx.mock
def test_narrate_brief_survives_a_non_json_body_returns_none_no_crash(tmp_path) -> None:
    respx.post(BASE).mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    adapter = OpenRouterPlannerAdapter(model=MODEL, api_key="k")
    conn = connect(tmp_path / "t.db")
    migrate(conn)

    result = narrate_brief(conn, 1, adapter, BRIEF_TEXT, soft_cap_usd=10.0, model=MODEL)

    assert result is None
    assert list(read_all(conn, "llm.call")) == []


@respx.mock
def test_plan_system_prompt_reflects_cfg_not_hardcoded_numbers() -> None:
    route = respx.post(BASE).mock(return_value=_response(json.dumps({"intents": []})))
    adapter = OpenRouterPlannerAdapter(model=MODEL, api_key="k")
    limits = PlannerLimits(
        reprice_min_price_usd=7.77,
        reprice_max_pct_change=0.31,
        seo_edit_min_lifetime_views=42,
        caption_max_len=321,
        pinterest_max_title_len=99,
        pinterest_max_description_len=444,
        pinterest_max_alt_text_len=333,
        pinterest_board_keys=["wall_art"],
    )

    adapter.plan("{}", [], limits)

    sent = json.loads(route.calls.last.request.content)
    system_prompt = sent["messages"][0]["content"]
    assert "7.77" in system_prompt
    assert "31" in system_prompt  # max_pct_change rendered as a percentage
    assert "42" in system_prompt
    assert "321" in system_prompt
    assert "99" in system_prompt
    assert "444" in system_prompt
    assert "333" in system_prompt
    assert "wall_art" in system_prompt
    # params guidance for the content-generating capabilities:
    assert "price_usd" in system_prompt
    assert "title" in system_prompt
    assert "tags" in system_prompt
    assert "description" in system_prompt
    assert "1-5000" in system_prompt
    assert "caption" in system_prompt
    assert "board_key" in system_prompt


@respx.mock
def test_missing_usage_yields_zero_tokens_and_cost() -> None:
    respx.post(BASE).mock(return_value=_response("ok"))
    adapter = OpenRouterPlannerAdapter(model=MODEL, api_key="k", est_cost_per_mtok=PRICING)

    result = adapter.narrate(BRIEF_TEXT)

    assert result.usage.prompt_tokens == 0
    assert result.usage.completion_tokens == 0
    assert result.usage.est_cost_usd == 0.0
