"""Regression for the M8b planner-path bug: `run(..., proposals=...)` must
never REPLACE a deterministic capability's own `propose()` output -- only
merge the planner's proposals in additionally. Production impact: with
`SHOPSTEWARD_LIVE_PLANNER=1`, 6 of 7 real `listing.renew`-eligible targets
were silently dropped because the LLM's intents that round only named 1 of
them. Entirely on StubCapability fakes -- zero network."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import run
from tests.pipeline.ops.helpers import seed_listing_observed_on
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
TODAY = datetime.now(UTC).date()


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


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


class PlannerOnlyCapability(StubCapability):
    """A capability like listing.seo_edit -- propose() always returns []
    (its own docstring: "planner-only"); only materialize() (via the
    planner's proposals list) can ever produce an action for it."""

    def propose(self, conn: object, user_id: int, cfg: object) -> list:
        return []


def test_deterministic_capabilitys_own_propose_output_survives_planner_mode(conn):
    """Reproduces the production bug: a target the deterministic propose()
    finds but the planner's list does NOT name must still be proposed."""
    cap = StubCapability(key="stub.renew", targets={"t-1": {"on": True}, "t-2": {"on": True}})
    register(cap)
    cfg = _cfg(enabled=True)

    deterministic = cap.propose(conn, USER_ID, cfg)
    assert {a.target_id for a in deterministic} == {"t-1", "t-2"}

    # The planner's list (as cli.py would pass it) only names t-1 -- t-2 was
    # exposed in candidate_target_ids but the LLM didn't act on it.
    planner_proposals = [a for a in deterministic if a.target_id == "t-1"]

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=planner_proposals)

    assert report.proposed == 2
    proposed_targets = {e.payload["target_id"] for e in read_all(conn, "action.proposed")}
    assert proposed_targets == {"t-1", "t-2"}


def test_planner_only_capability_unaffected_still_needs_planner_list(conn):
    """No regression: a capability whose own propose() always returns []
    must not suddenly propose anything on its own -- it only gets proposed
    via the planner-supplied list."""
    cap = PlannerOnlyCapability(key="stub.seo_edit", targets={"t-1": {"on": True}})
    register(cap)
    cfg = _cfg(enabled=True)

    assert cap.propose(conn, USER_ID, cfg) == []
    planner_action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        __import__(
            "shopsteward.adapters.planner.interface", fromlist=["ProposalIntent"]
        ).ProposalIntent(capability_key=cap.key, target_id="t-1", reason="llm reason"),
    )
    assert planner_action is not None

    # No planner proposal at all for this capability -> nothing proposed.
    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[])
    assert report.proposed == 0
    assert read_all(conn, "action.proposed") == []

    # Planner names it -> proposed exactly once, from the planner list only.
    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[planner_action])
    assert report.proposed == 1


def test_no_double_count_when_planner_list_repeats_the_deterministic_action(conn):
    """Same action_id from both the deterministic propose() and the planner
    list must be counted/logged exactly once, not twice."""
    cap = StubCapability(key="stub.dup", targets={"t-1": {"on": True}})
    register(cap)
    cfg = _cfg(enabled=True)

    deterministic = cap.propose(conn, USER_ID, cfg)
    assert len(deterministic) == 1

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=list(deterministic))

    assert report.proposed == 1
    proposed_events = [
        e for e in read_all(conn, "action.proposed") if e.payload["target_id"] == "t-1"
    ]
    assert len(proposed_events) == 1


def test_planner_alternate_for_a_target_already_covered_by_propose_is_dropped(conn):
    """The reprice bug: the planner's proposal for a target_id the
    capability's own propose() ALREADY covers must be dropped, even though
    its action_id differs (a different price -> a different inputs_hash ->
    a different action_id, so a plain action_id dedup does not catch it)."""
    cap = StubCapability(key="stub.reprice_like", targets={"t-1": {"on": True}})
    register(cap)
    cfg = _cfg(enabled=True)

    (deterministic,) = cap.propose(conn, USER_ID, cfg)
    planner_alternate = deterministic.model_copy(
        update={"action_id": "planner-alternate-for-t-1", "inputs_hash": "different-inputs"}
    )
    assert planner_alternate.action_id != deterministic.action_id
    assert planner_alternate.target_id == deterministic.target_id

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[planner_alternate])

    assert report.proposed == 1
    proposed_events = [
        e for e in read_all(conn, "action.proposed") if e.payload["target_id"] == "t-1"
    ]
    assert len(proposed_events) == 1
    assert proposed_events[0].payload["action_id"] == deterministic.action_id


LISTING_DIGITAL = 901


def test_reprice_real_capability_deterministic_default_wins_over_planner_alternate_price(conn):
    """Live-shaped regression for the exact reported bug: a real
    ListingReprice with a deterministic default price decrease for a
    listing, combined with a planner-materialized ALTERNATE price for the
    SAME listing, must produce exactly one reprice proposal -- the
    deterministic default, never both."""
    today = TODAY
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_DIGITAL,
        title="Loon Digital Download",
        day=today - timedelta(days=200),
        views=100,
        price_usd=20.0,
        state="active",
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_DIGITAL,
        title="Loon Digital Download",
        day=today - timedelta(days=1),
        views=100,
        price_usd=20.0,
        state="active",
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    register(cap)
    cfg = _cfg(enabled=True)

    (deterministic,) = cap.propose(conn, USER_ID, cfg)

    intent = ProposalIntent(
        capability_key="listing.reprice",
        target_id=str(LISTING_DIGITAL),
        params={"price_usd": 17.0},
        reason="planner's own alternate price for the same listing",
    )
    planner_alternate = cap.materialize(conn, USER_ID, cfg, intent)
    assert planner_alternate is not None
    assert planner_alternate.action_id != deterministic.action_id

    report = run(conn, USER_ID, cfg, [cap], today=today, proposals=[planner_alternate])

    assert report.proposed == 1
    proposed = [
        e
        for e in read_all(conn, "action.proposed")
        if e.payload["target_id"] == str(LISTING_DIGITAL)
    ]
    assert len(proposed) == 1
    assert proposed[0].payload["action_id"] == deterministic.action_id
    assert proposed[0].payload["params"]["price_usd"] == deterministic.params["price_usd"]
