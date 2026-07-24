import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.llm_ledger import monthly_spend

USER_ID = 1


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _call(conn, *, user_id=USER_ID, cost=1.0, purpose="commercial_triage"):
    append(
        conn,
        Event(
            user_id=user_id,
            type="llm.call",
            payload={
                "provider": "openrouter",
                "model": "m",
                "purpose": purpose,
                "input_tokens": 1,
                "output_tokens": 1,
                "est_cost_usd": cost,
            },
        ),
    )


def test_monthly_spend_sums_current_month_only(conn):
    _call(conn, cost=1.0)
    _call(conn, cost=2.5)
    assert monthly_spend(conn, USER_ID) == pytest.approx(3.5)


def test_monthly_spend_ignores_other_users(conn):
    _call(conn, user_id=USER_ID, cost=1.0)
    _call(conn, user_id=USER_ID + 1, cost=100.0)
    assert monthly_spend(conn, USER_ID) == pytest.approx(1.0)


def test_monthly_spend_ignores_other_months(conn):
    _call(conn, cost=5.0)
    assert monthly_spend(conn, USER_ID, month_prefix="1999-01") == 0.0


def test_monthly_spend_shared_across_purposes(conn):
    """The M3 vision cap and the M5a copy cap share one ledger (PRD §13
    decision 38): spend from either purpose counts toward the same total."""
    _call(conn, cost=4.0, purpose="commercial_triage")
    _call(conn, cost=3.0, purpose="listing_copy")
    assert monthly_spend(conn, USER_ID) == pytest.approx(7.0)


def test_monthly_spend_none_when_no_calls(conn):
    assert monthly_spend(conn, USER_ID) == 0.0
