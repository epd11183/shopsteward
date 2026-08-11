import pytest

from shopsteward.adapters.planner.fake import FakePlannerAdapter
from shopsteward.adapters.planner.interface import PlannerNarration, PlannerParseError, PlannerUsage
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops.planner import narrate_brief

USER_ID = 1
MODEL = "google/gemini-2.5-flash-lite"
BRIEF_TEXT = "ShopSteward -- 2026-08-11\n\nTHE SHOP\n  Revenue: $100.00"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _llm_calls(conn):
    return [e for e in read_all(conn, "llm.call")]


def test_narrate_brief_under_cap_returns_text_and_appends_one_event(conn):
    narration = PlannerNarration(
        text="Revenue held steady.",
        usage=PlannerUsage(prompt_tokens=100, completion_tokens=50, est_cost_usd=0.02),
    )
    adapter = FakePlannerAdapter([narration])

    result = narrate_brief(conn, USER_ID, adapter, BRIEF_TEXT, soft_cap_usd=10.0, model=MODEL)

    assert result == "Revenue held steady."
    calls = _llm_calls(conn)
    assert len(calls) == 1
    assert calls[0].user_id == USER_ID
    assert calls[0].payload["est_cost_usd"] == 0.02
    assert calls[0].payload["model"] == MODEL
    assert calls[0].payload["producer"] == "ops.planner.narrate"
    assert "token" not in str(calls[0].payload.get("api_key", ""))


def test_narrate_brief_over_cap_returns_none_and_appends_no_event(conn):
    append(
        conn,
        Event(user_id=USER_ID, type="llm.call", payload={"est_cost_usd": 10.0}),
    )
    before = len(_llm_calls(conn))
    adapter = FakePlannerAdapter()

    result = narrate_brief(conn, USER_ID, adapter, BRIEF_TEXT, soft_cap_usd=10.0, model=MODEL)

    assert result is None
    assert adapter.calls == []  # never even called -- gate checked first
    assert len(_llm_calls(conn)) == before  # no new event appended


def test_narrate_brief_transport_error_returns_none_no_crash(conn):
    adapter = FakePlannerAdapter([PlannerParseError("boom")])

    result = narrate_brief(conn, USER_ID, adapter, BRIEF_TEXT, soft_cap_usd=10.0, model=MODEL)

    assert result is None
    assert _llm_calls(conn) == []


def test_narrate_brief_does_not_mutate_brief_text(conn):
    adapter = FakePlannerAdapter()
    narrate_brief(conn, USER_ID, adapter, BRIEF_TEXT, soft_cap_usd=10.0, model=MODEL)
    assert adapter.calls == [BRIEF_TEXT]
