"""`listing.autorenew_off` (PR2, M8a spec §4, draft §3.1 row 5) -- the first
real autonomy capability. Entirely on FakeEtsyWriteAdapter, zero network.
The objective under test is exactly `dead_listings() ∩ (should_auto_renew==
True ∧ state=="active")` -- no expiry condition."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_DEAD_A = 501  # dead, active, auto-renew ON -- must be proposed
LISTING_DEAD_B = 502  # dead, active, auto-renew ON -- must be proposed
LISTING_HEALTHY = 503  # views growing in-window -- never proposed
LISTING_DEAD_ALREADY_OFF = 504  # dead, active, auto-renew already OFF -- never proposed
LISTING_DEAD_EXPIRED = 505  # dead, but state=expired -- never proposed


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
    """Flat views across the whole 180d dead-listing window, first observed
    200d ago (>= the default 90d min_observed_days) -- confirmed dead, not
    merely new/unmeasurable."""
    title = f"Listing {listing_id}"
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=200),
        views=50,
        state=state,
        should_auto_renew=should_auto_renew,
    )
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=100),
        views=50,
        state=state,
        should_auto_renew=should_auto_renew,
    )
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=1),
        views=50,
        state=state,
        should_auto_renew=should_auto_renew,
    )


def _seed_healthy(conn, listing_id: int):
    title = f"Listing {listing_id}"
    seed_listing_observed_on(
        conn, listing_id=listing_id, title=title, day=TODAY - timedelta(days=200), views=10
    )
    seed_listing_observed_on(
        conn, listing_id=listing_id, title=title, day=TODAY - timedelta(days=100), views=10
    )
    seed_listing_observed_on(
        conn, listing_id=listing_id, title=title, day=TODAY - timedelta(days=1), views=500
    )


def _seed_scenario(conn) -> None:
    _seed_dead(conn, LISTING_DEAD_A)
    _seed_dead(conn, LISTING_DEAD_B)
    _seed_healthy(conn, LISTING_HEALTHY)
    _seed_dead(conn, LISTING_DEAD_ALREADY_OFF, should_auto_renew=False)
    _seed_dead(conn, LISTING_DEAD_EXPIRED, state="expired")
    rebuild_core(conn)
    rebuild_ops(conn)


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def test_propose_targets_only_dead_active_autorenew_on_listings(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    cap = ListingAutorenewOff(fake)

    actions = cap.propose(conn, USER_ID, _cfg())

    target_ids = {a.target_id for a in actions}
    assert target_ids == {str(LISTING_DEAD_A), str(LISTING_DEAD_B)}
    for a in actions:
        assert a.capability == "listing.autorenew_off"
        assert a.target_type == "listing"
        assert a.estimated_cost_usd == 0.0
        assert a.undo_available is True
        assert a.reason  # one human sentence, non-empty


def test_propose_skips_a_malformed_observed_event_instead_of_crashing(conn):
    _seed_scenario(conn)
    # A malformed later observation for LISTING_DEAD_B (missing required
    # "state") must not crash propose() -- it should fall back to the last
    # VALID snapshot (still should_auto_renew=True/active from _seed_dead),
    # and LISTING_DEAD_A must still be proposed untouched.
    conn.execute(
        "INSERT INTO events (user_id, type, payload) VALUES (?, ?, ?)",
        (
            USER_ID,
            "etsy.listing.observed",
            f'{{"listing_id": {LISTING_DEAD_B}, "title": "broken"}}',
        ),
    )
    conn.commit()
    fake = FakeEtsyWriteAdapter()
    cap = ListingAutorenewOff(fake)

    actions = cap.propose(conn, USER_ID, _cfg())

    target_ids = {a.target_id for a in actions}
    assert target_ids == {str(LISTING_DEAD_A), str(LISTING_DEAD_B)}


def test_register_succeeds_capability_has_undo_and_t1_ceiling():
    fake = FakeEtsyWriteAdapter()
    cap = ListingAutorenewOff(fake)

    register(cap)

    assert REGISTRY["listing.autorenew_off"] is cap
    assert cap.max_tier == Tier.NOTIFY
    assert callable(cap.undo)


def test_e2e_approve_executes_the_approved_listing_and_undo_restores_it(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_A, should_auto_renew=True, state="active")
    fake.seed_listing(LISTING_DEAD_B, should_auto_renew=True, state="active")
    cap = ListingAutorenewOff(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert report.proposed == 2
    assert report.executed == 0  # fresh capability starts at T2/PROPOSE -- waits on the operator

    proposed = {
        e.payload["target_id"]: e.payload["action_id"] for e in read_all(conn, "action.proposed")
    }
    action_id_a = proposed[str(LISTING_DEAD_A)]

    approved = approve_action(
        conn, USER_ID, action_id_a, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )

    assert approved.executed == 1
    assert fake.listings[LISTING_DEAD_A]["should_auto_renew"] is False
    assert fake.listings[LISTING_DEAD_B]["should_auto_renew"] is True  # never touched
    update_calls = [c for c in fake.calls if c[0] == "update_listing"]
    assert len(update_calls) == 1
    assert update_calls[0][1]["listing_id"] == LISTING_DEAD_A
    assert update_calls[0][1]["fields"] == {"should_auto_renew": False}

    executed_events = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id_a
    ]
    assert len(executed_events) == 1
    assert executed_events[0].payload["before"] == {"should_auto_renew": True}
    assert executed_events[0].payload["after"] == {"should_auto_renew": False}
    assert executed_events[0].payload["cost_usd"] == 0.0

    undo_action(conn, USER_ID, action_id_a, [cap], live_autonomy=True)

    assert fake.listings[LISTING_DEAD_A]["should_auto_renew"] is True
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id_a]
    assert len(undone) == 1
    assert undone[0].payload["restored_to"] == {"should_auto_renew": True}

    # capability demotes back to PROPOSE on undo, counters reset
    state = capability_states(conn, USER_ID)["listing.autorenew_off"]
    assert state.tier == Tier.PROPOSE
    assert state.undos == 0  # reset by the demotion the undo itself triggered


def test_e2e_second_approval_hits_the_daily_cap(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_A, should_auto_renew=True, state="active")
    fake.seed_listing(LISTING_DEAD_B, should_auto_renew=True, state="active")
    cap = ListingAutorenewOff(fake)
    cfg = _cfg(enabled=True, daily_action_cap=1, weekly_catalog_pct_cap=1.0)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_ids = [e.payload["action_id"] for e in read_all(conn, "action.proposed")]

    first = approve_action(
        conn, USER_ID, action_ids[0], [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    second = approve_action(
        conn, USER_ID, action_ids[1], [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )

    assert first.executed == 1
    assert second.executed == 0
    refused = [
        e for e in read_all(conn, "action.refused") if e.payload["action_id"] == action_ids[1]
    ]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "daily_cap"


def test_e2e_rerun_same_day_is_idempotent(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    cap = ListingAutorenewOff(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    first = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert first.proposed == 2

    second = run(conn, USER_ID, cfg, [cap], today=TODAY)

    assert second.proposed == 0
    assert second.skipped_idempotent == 2
    assert len(read_all(conn, "action.proposed")) == 2


def test_e2e_zero_cost_passes_a_zero_dollar_budget_cap(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_A, should_auto_renew=True, state="active")
    fake.seed_listing(LISTING_DEAD_B, should_auto_renew=True, state="active")
    cap = ListingAutorenewOff(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0, monthly_spend_cap_usd=0.00)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    report = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )

    assert report.executed == 1  # $0.00 estimated cost never trips the $0.00 cap
    refused = [e for e in read_all(conn, "action.refused") if e.payload["action_id"] == action_id]
    assert refused == []


def test_e2e_every_executed_action_has_a_preceding_approved_and_no_secret_leaks(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_A, should_auto_renew=True, state="active")
    fake.seed_listing(LISTING_DEAD_B, should_auto_renew=True, state="active")
    cap = ListingAutorenewOff(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True)

    events = read_all(conn)
    first_approved_id: dict[str, int] = {}
    for e in events:
        if e.type == "action.approved":
            first_approved_id.setdefault(e.payload["action_id"], e.id)
    executed = [e for e in events if e.type == "action.executed"]
    assert len(executed) == 1
    for e in executed:
        approved_id = first_approved_id.get(e.payload["action_id"])
        assert approved_id is not None
        assert approved_id < e.id

    import json

    banned = ("token", "api_key", "apikey", "secret", "signed_url", "access_token", "refresh_token")
    for e in events:
        blob = json.dumps(e.payload).lower()
        for word in banned:
            assert word not in blob, f"event {e.type} payload leaked {word!r}: {blob}"


def test_fake_update_listing_applies_should_auto_renew_and_records_the_call():
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(999, should_auto_renew=True, state="active")

    from shopsteward.adapters.etsy.models import EtsyListingUpdate

    updated = fake.update_listing(999, EtsyListingUpdate(should_auto_renew=False))

    assert updated.should_auto_renew is False
    assert fake.listings[999]["should_auto_renew"] is False
    assert ("update_listing", {"listing_id": 999, "fields": {"should_auto_renew": False}}) in (
        fake.calls
    )
