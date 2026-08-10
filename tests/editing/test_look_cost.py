from shopsteward.adapters.look.interface import LookUsage
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.editing.look_cost import append_llm_call, month_look_cost

USER = 1


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def test_append_and_sum_current_month():
    c = _conn()
    append_llm_call(c, USER, LookUsage(model="m", input_tokens=10, output_tokens=20,
                                       est_cost_usd=0.03), description="a look")
    append_llm_call(c, USER, LookUsage(model="m", est_cost_usd=0.05), description="b look")
    month = read_all(c, "llm.call")[0].created_at[:7]
    assert round(month_look_cost(c, USER, month), 2) == 0.08


def test_other_month_excluded():
    c = _conn()
    append_llm_call(c, USER, LookUsage(model="m", est_cost_usd=1.0), description="x")
    assert month_look_cost(c, USER, "1999-01") == 0.0


def test_none_cost_counts_as_zero():
    c = _conn()
    append_llm_call(c, USER, LookUsage(model="m", est_cost_usd=None), description="x")
    month = read_all(c, "llm.call")[0].created_at[:7]
    assert month_look_cost(c, USER, month) == 0.0
