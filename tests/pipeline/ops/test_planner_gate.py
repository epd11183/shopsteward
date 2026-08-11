"""M8b slice 2 -- the planner validation gate (design §2/§6, the
safety-critical test of this milestone). An LLM proposal for an unknown/
prohibited/hallucinated action must be dropped BEFORE it can become an
`action.proposed`, and every drop is logged as `planner.intent_dropped`.
Entirely on fakes -- zero network."""

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

import shopsteward.pipeline.listings.push as push_mod
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.fake import FakePlannerAdapter
from shopsteward.adapters.planner.interface import PlannerParseError, ProposalIntent
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.planner import plan_proposals
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import run
from tests.pipeline.ops.helpers import seed_listing_observed_on
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
TODAY = datetime.now(UTC).date()
LISTING_DEAD_A = 601

runner_cli = CliRunner()


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_dead(conn, listing_id: int, *, should_auto_renew: bool = True, state: str = "active"):
    title = f"Listing {listing_id}"
    for days_ago in (200, 100, 1):
        seed_listing_observed_on(
            conn,
            listing_id=listing_id,
            title=title,
            day=TODAY - timedelta(days=days_ago),
            views=50,
            state=state,
            should_auto_renew=should_auto_renew,
        )


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def _dropped(conn):
    return read_all(conn, "planner.intent_dropped")


# --- the mandatory safety test (design §6) -----------------------------------


def test_unknown_prohibited_and_hallucinated_intents_are_all_dropped(conn):
    _seed_dead(conn, LISTING_DEAD_A)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingAutorenewOff(FakeEtsyWriteAdapter())
    register(cap)
    cfg = _cfg(enabled=True)

    adapter = FakePlannerAdapter(
        plan=[
            ProposalIntent(
                capability_key="listing.send_buyer_message", target_id="42", reason="reply to buyer"
            ),
            ProposalIntent(
                capability_key="listing.autorenew_off",
                target_id="999999",
                reason="invented listing",
            ),
        ]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert len(adapter.plan_calls) == 1  # plan() really ran, not short-circuited before it
    assert proposals == []
    assert read_all(conn, "action.proposed") == []  # govern() is never reached

    reasons = {(e.payload["reason"], e.payload["capability_key"]) for e in _dropped(conn)}
    assert ("customer_contact_barred", "listing.send_buyer_message") in reasons
    assert ("ungrounded", "listing.autorenew_off") in reasons
    assert len(_dropped(conn)) == 2


# --- a valid intent lands at T2, never auto-executed -------------------------


def test_valid_intent_produces_one_proposed_action_that_lands_at_t2(conn):
    _seed_dead(conn, LISTING_DEAD_A)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingAutorenewOff(FakeEtsyWriteAdapter())
    register(cap)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    deterministic_targets = {a.target_id for a in cap.propose(conn, USER_ID, cfg)}
    assert deterministic_targets == {str(LISTING_DEAD_A)}

    adapter = FakePlannerAdapter(
        plan=[
            ProposalIntent(
                capability_key="listing.autorenew_off",
                target_id=str(LISTING_DEAD_A),
                reason="the LLM's own sentence -- must never become the audit reason",
            )
        ]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert len(proposals) == 1
    assert proposals[0].target_id == str(LISTING_DEAD_A)
    # materialize()'s own SQL-derived reason, NOT the LLM's sentence.
    assert "LLM's own sentence" not in proposals[0].reason
    assert proposals[0].reason == cap.propose(conn, USER_ID, cfg)[0].reason

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=proposals)
    assert report.proposed == 1
    assert report.executed == 0  # T2/PROPOSE -- never auto-executed

    proposed_events = read_all(conn, "action.proposed")
    assert len(proposed_events) == 1
    assert proposed_events[0].payload["tier"] == int(Tier.PROPOSE)


def test_policy_unverified_intent_is_dropped(conn):
    rebuild_ops(conn)
    cfg = _cfg(enabled=True)
    cap = StubCapability(key="stub.unverified", policy_verified=False)
    register(cap)

    adapter = FakePlannerAdapter(
        plan=[ProposalIntent(capability_key="stub.unverified", target_id="t-1", reason="try")]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert proposals == []
    dropped = _dropped(conn)
    assert len(dropped) == 1
    assert dropped[0].payload["reason"] == "policy_unverified"


def test_per_capability_cap_keeps_the_llms_first_and_drops_the_rest(conn):
    rebuild_ops(conn)
    cfg = _cfg(enabled=True)
    cfg.autonomy.planner_max_per_capability_per_run = 1
    cap = StubCapability(key="stub.cap", targets={"t-1": {"on": True}, "t-2": {"on": True}})
    register(cap)

    adapter = FakePlannerAdapter(
        plan=[
            ProposalIntent(capability_key="stub.cap", target_id="t-1", reason="first"),
            ProposalIntent(capability_key="stub.cap", target_id="t-2", reason="second"),
        ]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert [p.target_id for p in proposals] == ["t-1"]
    dropped = _dropped(conn)
    assert len(dropped) == 1
    assert dropped[0].payload["reason"] == "per_run_cap"
    assert dropped[0].payload["target_id"] == "t-2"


def test_over_monthly_cost_cap_returns_empty_with_no_plan_call(conn):
    append(conn, Event(user_id=USER_ID, type="llm.call", payload={"est_cost_usd": 10.0}))
    cfg = _cfg(enabled=True)
    cap = StubCapability()
    register(cap)
    adapter = FakePlannerAdapter(
        plan=[ProposalIntent(capability_key=cap.key, target_id="t-1", reason="x")]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert proposals == []
    assert adapter.plan_calls == []  # never even called -- cost gate checked first
    assert len(read_all(conn, "llm.call")) == 1  # only the seeded one, no new call
    assert _dropped(conn) == []


def test_transport_error_in_plan_returns_empty_no_crash(conn):
    rebuild_ops(conn)
    cfg = _cfg(enabled=True)
    cap = StubCapability()
    register(cap)
    adapter = FakePlannerAdapter(plan=PlannerParseError("boom"))

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert proposals == []
    assert read_all(conn, "llm.call") == []


def test_materialize_and_propose_share_grounding_and_agree(conn):
    _seed_dead(conn, LISTING_DEAD_A)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingAutorenewOff(FakeEtsyWriteAdapter())
    cfg = _cfg()

    proposed = cap.propose(conn, USER_ID, cfg)
    assert proposed
    for action in proposed:
        intent = ProposalIntent(capability_key=cap.key, target_id=action.target_id, reason="x")
        assert cap.materialize(conn, USER_ID, cfg, intent) == action

    bad_intent = ProposalIntent(capability_key=cap.key, target_id="not-a-real-target", reason="x")
    assert cap.materialize(conn, USER_ID, cfg, bad_intent) is None


# --- CLI: default path unchanged; gate-closed never builds an adapter -------


def _seed_cfg(conn, **autonomy_overrides) -> None:
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="opsconfig.seeded",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "defaults",
            },
        ),
    )


def test_cli_ops_run_planner_disabled_uses_the_deterministic_path(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_AUTONOMY", raising=False)
    monkeypatch.delenv("SHOPSTEWARD_LIVE_PLANNER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    conn = connect(db)
    migrate(conn)
    _seed_cfg(conn, enabled=True, weekly_catalog_pct_cap=1.0)
    _seed_dead(conn, LISTING_DEAD_A)
    rebuild_core(conn)
    rebuild_ops(conn)
    conn.close()

    fake = FakeEtsyWriteAdapter()
    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", lambda *, live: fake)

    result = runner_cli.invoke(app, ["ops", "run"])

    assert result.exit_code == 0, result.output
    assert "planner: off (deterministic)" in result.output

    conn = connect(db)
    assert len(read_all(conn, "action.proposed")) == 1


def test_cli_ops_run_planner_enabled_but_gate_closed_never_builds_an_adapter(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_AUTONOMY", raising=False)
    monkeypatch.delenv("SHOPSTEWARD_LIVE_PLANNER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    conn = connect(db)
    migrate(conn)
    _seed_cfg(conn, enabled=True, planner_enabled=True, weekly_catalog_pct_cap=1.0)
    _seed_dead(conn, LISTING_DEAD_A)
    rebuild_core(conn)
    rebuild_ops(conn)
    conn.close()

    fake = FakeEtsyWriteAdapter()
    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", lambda *, live: fake)

    import shopsteward.adapters.planner.openrouter as planner_openrouter_mod

    def _no_live_adapter(*args, **kwargs):
        raise AssertionError("OpenRouterPlannerAdapter must not be built when the gate is closed")

    monkeypatch.setattr(planner_openrouter_mod, "OpenRouterPlannerAdapter", _no_live_adapter)

    result = runner_cli.invoke(app, ["ops", "run"])

    assert result.exit_code == 0, result.output
    assert "planner: off (deterministic)" in result.output

    conn = connect(db)
    assert len(read_all(conn, "action.proposed")) == 1
