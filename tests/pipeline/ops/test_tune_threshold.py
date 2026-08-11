"""`ops.tune_threshold` (PR4, M8a spec §4, draft #36) -- the feedback-loop
capability that tunes `dead_listing.min_observed_days` down when it's set
higher than the catalog's longest observation span (guaranteeing the
dead-listing analysis is always empty). T2/PROPOSE only, no adapter, no
network -- entirely against a synthetic event log."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.tune_threshold import OpsTuneThreshold
from shopsteward.pipeline.ops.models import ProposedAction, Tier
from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_A = 701


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


def _seed_listing(conn, listing_id: int, first_seen_days_ago: int) -> None:
    title = f"Listing {listing_id}"
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=first_seen_days_ago),
        views=10,
    )
    seed_listing_observed_on(
        conn, listing_id=listing_id, title=title, day=TODAY - timedelta(days=1), views=15
    )


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def test_propose_fires_when_min_observed_exceeds_the_longest_span(conn):
    # default min_observed_days=90; longest span 40d -- 40 >= 14, so it fires.
    _seed_listing(conn, LISTING_A, first_seen_days_ago=40)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()
    cfg = _cfg()

    actions = cap.propose(conn, USER_ID, cfg)

    assert len(actions) == 1
    action = actions[0]
    assert action.capability == "ops.tune_threshold"
    assert action.target_type == "ops_config"
    assert action.target_id == cfg.name
    assert action.estimated_cost_usd == 0.0
    assert action.undo_available is True
    assert "min_observed_days=90" in action.reason
    assert "40d" in action.reason
    assert "min_observed_days=20" in action.reason  # max(7, 40 // 2)


def test_no_proposal_when_config_already_fits(conn):
    # span 40d, min_observed_days lowered to 30 (<= span) -- analysis can fire.
    _seed_listing(conn, LISTING_A, first_seen_days_ago=40)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()
    cfg = _cfg()
    cfg.dead_listing.min_observed_days = 30

    assert cap.propose(conn, USER_ID, cfg) == []


def test_no_proposal_when_span_below_the_minimum_to_tune(conn):
    # span 10d < _MIN_SPAN_TO_TUNE=14, even though min_observed_days=90 > span.
    _seed_listing(conn, LISTING_A, first_seen_days_ago=10)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()

    assert cap.propose(conn, USER_ID, _cfg()) == []


def test_no_proposal_with_zero_listings(conn):
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()

    assert cap.propose(conn, USER_ID, _cfg()) == []


def test_register_is_t2_and_has_undo():
    cap = OpsTuneThreshold()

    register(cap)

    assert REGISTRY["ops.tune_threshold"] is cap
    assert cap.max_tier == Tier.PROPOSE
    assert callable(cap.undo)


def test_e2e_proposes_but_never_auto_executes_then_approve_undo_and_idempotent_rerun(conn):
    _seed_listing(conn, LISTING_A, first_seen_days_ago=40)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert report.proposed == 1
    assert report.executed == 0  # T2 -- never auto-executed

    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    assert approved.executed == 1

    executed_events = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id
    ]
    assert len(executed_events) == 1
    assert executed_events[0].payload["before"] == {"min_observed_days": 90}
    assert executed_events[0].payload["after"] == {"min_observed_days": 20}
    assert executed_events[0].payload["cost_usd"] == 0.0

    updated_events = [e for e in read_all(conn, "opsconfig.updated")]
    assert len(updated_events) == 1
    assert updated_events[0].payload["source"] == "autonomy"
    assert updated_events[0].payload["config"]["dead_listing"]["min_observed_days"] == 20

    rebuild_ops(conn)
    assert ops_config.get_ops_config(conn, USER_ID).dead_listing.min_observed_days == 20

    # self-limiting: new value <= span, so a re-run proposes nothing new.
    rerun = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert rerun.proposed == 0

    undo_action(conn, USER_ID, action_id, [cap])

    undo_updates = [
        e for e in read_all(conn, "opsconfig.updated") if e.payload["source"] == "autonomy_undo"
    ]
    assert len(undo_updates) == 1
    assert undo_updates[0].payload["config"]["dead_listing"]["min_observed_days"] == 90

    rebuild_ops(conn)
    assert ops_config.get_ops_config(conn, USER_ID).dead_listing.min_observed_days == 90

    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id]
    assert len(undone) == 1
    assert undone[0].payload["restored_to"] == {"min_observed_days": 90}

    state = capability_states(conn, USER_ID)["ops.tune_threshold"]
    assert state.tier == Tier.PROPOSE
    assert state.undos == 0  # reset by the demotion the undo itself triggered


def test_execute_is_a_safe_noop_when_config_no_longer_differs(conn):
    _seed_listing(conn, LISTING_A, first_seen_days_ago=40)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()
    cfg = ops_config.get_ops_config(conn, USER_ID)

    actions = cap.propose(conn, USER_ID, cfg)
    (action,) = actions

    # Config already moved past the trigger since propose (simulate an
    # operator having already applied a fitting value) -- execute() must
    # append nothing and report before == after.
    fitted = cfg.model_copy(deep=True)
    fitted.dead_listing.min_observed_days = 20

    append(
        conn,
        Event(
            user_id=USER_ID,
            type="opsconfig.updated",
            payload={
                "name": cfg.name,
                "config": fitted.model_dump(by_alias=True),
                "source": "operator",
            },
        ),
    )
    rebuild_ops(conn)

    before_count = len(read_all(conn, "opsconfig.updated"))
    result = cap.execute(conn, USER_ID, action)

    assert result.before == result.after == {"min_observed_days": 20}
    assert len(read_all(conn, "opsconfig.updated")) == before_count  # nothing appended


def test_no_secret_in_any_payload_and_append_only(conn):
    _seed_listing(conn, LISTING_A, first_seen_days_ago=40)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    undo_action(conn, USER_ID, action_id, [cap])

    import json

    banned = ("token", "api_key", "apikey", "secret", "signed_url", "access_token", "refresh_token")
    events = read_all(conn)
    for e in events:
        blob = json.dumps(e.payload).lower()
        for word in banned:
            assert word not in blob, f"event {e.type} payload leaked {word!r}: {blob}"
        assert e.user_id == USER_ID


def _insert_action_proposed_on(conn, action: ProposedAction, day) -> None:
    """Same raw-INSERT-with-explicit-created_at pattern as
    helpers.seed_listing_observed_on -- still a plain INSERT (append-only),
    just backdating the row so a test can pin a proposal to a specific day."""
    created_at = f"{day.isoformat()}T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (USER_ID, "action.proposed", action.model_dump_json(), created_at),
    )
    conn.commit()


def test_execute_applies_the_value_from_the_proposal_date_not_wall_clock_now(conn):
    """Regression: execute() must not re-derive `span`/`new_min_observed`
    from today's wall clock -- the catalog's observed span only grows with
    time, so an operator approving days after the proposal would otherwise
    get a DIFFERENT (larger) min_observed_days than the one named in the
    reason they approved."""
    proposal_day = TODAY - timedelta(days=6)
    # As of wall-clock TODAY the span is 46d (naive recompute -> new=23).
    # As of the pinned proposal_day (6d earlier) the span was only 40d
    # (-> new=20, matching what the operator actually saw proposed).
    _seed_listing(conn, LISTING_A, first_seen_days_ago=46)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()
    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert cfg.dead_listing.min_observed_days == 90

    action = ProposedAction(
        action_id="pinned-test-action",
        capability=cap.key,
        target_type="ops_config",
        target_id=cfg.name,
        tier=Tier.PROPOSE,
        reason="min_observed_days=90 exceeds your longest observation (40d); propose 20.",
        inputs_hash="irrelevant-for-this-test",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(proposal_day + timedelta(days=14)).isoformat(),
    )
    _insert_action_proposed_on(conn, action, proposal_day)

    result = cap.execute(conn, USER_ID, action)

    assert result.after == {"min_observed_days": 20}  # the proposal-day value, not 23

    updated = [e for e in read_all(conn, "opsconfig.updated") if e.payload["source"] == "autonomy"]
    assert len(updated) == 1
    assert updated[0].payload["config"]["dead_listing"]["min_observed_days"] == 20
