"""`listing.deactivate` (M8b slice 4b, design §4/§8 slice 4, draft #7) --
retire a dead listing (state active->inactive). T1 ceiling (max_tier =
Tier.NOTIFY), ships/lands at T2 per the chassis default -- promotable to T1,
unlike reprice/seo_edit which cap at T2. Only listing STATE is ever
changed, never title/tags/price/SKU -- a dedicated adapter method
`update_listing_state` keeps that write-safety invariant crisp (SEO edit /
reprice use EtsyListingUpdate, which stays state-free). BOTH digital and POD
listings are eligible -- deactivate never touches SKUs/variation/price, so
POD-first is preserved regardless of product type (contrast reprice).
Entirely on FakeEtsyWriteAdapter, zero network.

The portfolio cap (governor.py's existing weekly_catalog_pct_cap) is the
real control here -- this file CONFIRMS the governor refuses
over-deactivation rather than reinventing any cap logic."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListingUpdate
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.deactivate import ListingDeactivate
from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
from shopsteward.pipeline.ops.capabilities.seo_edit import ListingSeoEdit
from shopsteward.pipeline.ops.models import ProposedAction, Tier
from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_DEAD_DIGITAL = 901  # dead, active, digital -- must be proposed
LISTING_DEAD_CANVAS = 902  # dead, active, POD -- must ALSO be proposed (contrast reprice)
LISTING_HEALTHY = 903  # views growing -- never proposed
LISTING_DEAD_ALREADY_INACTIVE = 904  # dead, but already inactive -- never proposed
LISTING_DEAD_EXPIRED = 905  # dead, but state=expired -- never proposed


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


def _seed_dead(conn, listing_id: int, title: str = "", *, state: str = "active"):
    """Flat views across the whole 180d dead-listing window, first observed
    200d ago (>= the default 90d min_observed_days) -- confirmed dead, not
    merely new/unmeasurable (autorenew.py test precedent)."""
    title = title or f"Listing {listing_id}"
    for offset in (200, 100, 1):
        seed_listing_observed_on(
            conn,
            listing_id=listing_id,
            title=title,
            day=TODAY - timedelta(days=offset),
            views=50,
            state=state,
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
    _seed_dead(conn, LISTING_DEAD_DIGITAL, "Loon at Dusk Digital Download")
    _seed_dead(conn, LISTING_DEAD_CANVAS, "Loon at Dusk Canvas Print")
    _seed_healthy(conn, LISTING_HEALTHY)
    _seed_dead(conn, LISTING_DEAD_ALREADY_INACTIVE, state="inactive")
    _seed_dead(conn, LISTING_DEAD_EXPIRED, state="expired")
    rebuild_core(conn)
    rebuild_ops(conn)


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


# --- 1. the load-bearing portfolio-cap test ---------------------------------


def test_portfolio_cap_refuses_over_deactivation_and_the_listing_stays_active(conn):
    """Three dead+active listings; weekly_catalog_pct_cap set so only the
    first deactivation in the week fits. The second approved deactivation
    must be action.refused{portfolio_cap}, and the fake must show that
    listing STILL active -- the governor's existing PORTFOLIO_CAP is the
    real control, not anything this capability invents."""
    _seed_dead(conn, 911, "Print A")
    _seed_dead(conn, 912, "Print B")
    _seed_dead(conn, 913, "Print C")
    rebuild_core(conn)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    for lid in (911, 912, 913):
        fake.seed_listing(lid, state="active")
    cap = ListingDeactivate(fake)
    # active_listing_count == 3 (proj_listings, unaffected by fake writes) --
    # 1/3 == 0.333 passes a 0.4 cap, 2/3 == 0.667 does not.
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=0.4)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_ids = sorted(
        (e.payload["target_id"], e.payload["action_id"]) for e in read_all(conn, "action.proposed")
    )
    assert len(action_ids) == 3

    first = approve_action(conn, USER_ID, action_ids[0][1], [cap], cfg=cfg, today=TODAY)
    second = approve_action(conn, USER_ID, action_ids[1][1], [cap], cfg=cfg, today=TODAY)

    assert first.executed == 1
    assert second.executed == 0
    refused_target = action_ids[1][0]
    refused = [
        e for e in read_all(conn, "action.refused") if e.payload["action_id"] == action_ids[1][1]
    ]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "portfolio_cap"
    assert fake.listings[int(refused_target)]["state"] == "active"  # never touched


# --- 2. eligibility: both digital and POD, active-only ----------------------


def test_propose_targets_dead_active_listings_regardless_of_product_type(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    cap = ListingDeactivate(fake)

    actions = cap.propose(conn, USER_ID, _cfg())

    target_ids = {a.target_id for a in actions}
    assert target_ids == {str(LISTING_DEAD_DIGITAL), str(LISTING_DEAD_CANVAS)}  # POD IS eligible
    for a in actions:
        assert a.capability == "listing.deactivate"
        assert a.target_type == "listing"
        assert a.estimated_cost_usd == 0.0
        assert a.undo_available is True
        assert a.reason


def test_materialize_shares_candidates_with_propose(conn):
    _seed_scenario(conn)
    cap = ListingDeactivate(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent(
        capability_key="listing.deactivate",
        target_id=str(LISTING_DEAD_DIGITAL),
        params={},
        reason="dead listing, deactivate it",
    )
    action = cap.materialize(conn, USER_ID, cfg, intent)
    assert action is not None
    assert action.target_id == str(LISTING_DEAD_DIGITAL)

    for hallucinated in (str(LISTING_HEALTHY), "999999"):
        bad_intent = ProposalIntent(
            capability_key="listing.deactivate",
            target_id=hallucinated,
            params={},
            reason="hallucinated target",
        )
        assert cap.materialize(conn, USER_ID, cfg, bad_intent) is None


def test_already_inactive_listing_is_not_eligible(conn):
    _seed_scenario(conn)
    cap = ListingDeactivate(FakeEtsyWriteAdapter())

    target_ids = {a.target_id for a in cap.propose(conn, USER_ID, _cfg())}
    assert str(LISTING_DEAD_ALREADY_INACTIVE) not in target_ids


def test_expired_listing_is_not_eligible(conn):
    _seed_scenario(conn)
    cap = ListingDeactivate(FakeEtsyWriteAdapter())

    target_ids = {a.target_id for a in cap.propose(conn, USER_ID, _cfg())}
    assert str(LISTING_DEAD_EXPIRED) not in target_ids


def test_healthy_listing_is_not_eligible(conn):
    _seed_scenario(conn)
    cap = ListingDeactivate(FakeEtsyWriteAdapter())

    target_ids = {a.target_id for a in cap.propose(conn, USER_ID, _cfg())}
    assert str(LISTING_HEALTHY) not in target_ids


# --- 3. registration ---------------------------------------------------------


def test_register_succeeds_capability_has_undo_and_t1_ceiling():
    cap = ListingDeactivate(FakeEtsyWriteAdapter())

    register(cap)

    assert REGISTRY["listing.deactivate"] is cap
    assert cap.max_tier == Tier.NOTIFY
    assert callable(cap.undo)


# --- 4. E2E via runner: T2 landing, approve, undo, idempotent re-run --------


def test_e2e_approve_flips_state_and_undo_reactivates(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_DIGITAL, state="active")
    fake.seed_listing(LISTING_DEAD_CANVAS, state="active")
    cap = ListingDeactivate(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert report.proposed == 2
    assert report.executed == 0  # fresh capability starts at T2/PROPOSE -- waits on the operator

    proposed = {
        e.payload["target_id"]: e.payload["action_id"] for e in read_all(conn, "action.proposed")
    }
    action_id = proposed[str(LISTING_DEAD_DIGITAL)]

    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    assert approved.executed == 1
    assert fake.listings[LISTING_DEAD_DIGITAL]["state"] == "inactive"
    assert fake.listings[LISTING_DEAD_CANVAS]["state"] == "active"  # never touched
    state_calls = [c for c in fake.calls if c[0] == "update_listing_state"]
    assert len(state_calls) == 1
    assert state_calls[0][1] == {"listing_id": LISTING_DEAD_DIGITAL, "state": "inactive"}

    executed_events = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id
    ]
    assert len(executed_events) == 1
    assert executed_events[0].payload["before"] == {"state": "active"}
    assert executed_events[0].payload["after"] == {"state": "inactive"}
    assert executed_events[0].payload["cost_usd"] == 0.0

    undo_action(conn, USER_ID, action_id, [cap])

    assert fake.listings[LISTING_DEAD_DIGITAL]["state"] == "active"
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id]
    assert len(undone) == 1
    assert undone[0].payload["restored_to"] == {"state": "active"}

    state = capability_states(conn, USER_ID)["listing.deactivate"]
    assert state.tier == Tier.PROPOSE
    assert state.undos == 0  # reset by the demotion the undo itself triggered


def test_e2e_rerun_same_day_is_idempotent(conn):
    _seed_scenario(conn)
    cap = ListingDeactivate(FakeEtsyWriteAdapter())
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    first = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert first.proposed == 2

    second = run(conn, USER_ID, cfg, [cap], today=TODAY)

    assert second.proposed == 0
    assert second.skipped_idempotent == 2
    assert len(read_all(conn, "action.proposed")) == 2


# --- 5. execute-time re-validation -------------------------------------------


def test_execute_refuses_a_hand_forged_action_for_an_already_inactive_listing(conn):
    _seed_dead(conn, LISTING_DEAD_ALREADY_INACTIVE, state="inactive")
    rebuild_core(conn)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_ALREADY_INACTIVE, state="inactive")
    cap = ListingDeactivate(fake)

    forged = ProposedAction(
        action_id="forged-deactivate",
        capability="listing.deactivate",
        target_type="listing",
        target_id=str(LISTING_DEAD_ALREADY_INACTIVE),
        tier=Tier.NOTIFY,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    state_calls = [c for c in fake.calls if c[0] == "update_listing_state"]
    assert state_calls == []  # never re-deactivated something already changed


# --- 6. adapter: update_listing_state ----------------------------------------


def test_fake_update_listing_state_rejects_a_bad_state_value():
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(999, state="active")

    with pytest.raises(ValueError):
        fake.update_listing_state(999, "draft")


def test_fake_update_listing_state_404s_on_unknown_id():
    from shopsteward.adapters.etsy.interface import EtsyWriteError

    fake = FakeEtsyWriteAdapter()

    with pytest.raises(EtsyWriteError):
        fake.update_listing_state(123456, "inactive")


def test_fake_update_listing_state_applies_and_records_the_call():
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(999, state="active")

    fake.update_listing_state(999, "inactive")

    assert fake.listings[999]["state"] == "inactive"
    assert ("update_listing_state", {"listing_id": 999, "state": "inactive"}) in fake.calls


# --- 7. promotable to T1 (unlike reprice/seo_edit, which cap at T2) --------


def test_promotion_t2_to_t1_can_fire_unlike_reprice_and_seo_edit(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_DIGITAL, state="active")
    fake.seed_listing(LISTING_DEAD_CANVAS, state="active")
    cap = ListingDeactivate(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    cfg.autonomy.ladder.promote_approvals = 2
    cfg.autonomy.ladder.promote_min_days = 1

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_ids = [e.payload["action_id"] for e in read_all(conn, "action.proposed")]

    approve_action(conn, USER_ID, action_ids[0], [cap], cfg=cfg, today=TODAY)
    later = TODAY + timedelta(days=1)
    approve_action(conn, USER_ID, action_ids[1], [cap], cfg=cfg, today=later)

    promoted = [e for e in read_all(conn, "capability.") if e.type == "capability.promoted"]
    assert len(promoted) == 1
    assert promoted[0].payload["capability"] == "listing.deactivate"
    assert promoted[0].payload["to_tier"] == int(Tier.NOTIFY)

    state = capability_states(conn, USER_ID)["listing.deactivate"]
    assert state.tier == Tier.NOTIFY


# --- 8. no secret leaks, append-only, user_id on every event ----------------


def test_no_secret_in_any_payload_and_append_only(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DEAD_DIGITAL, state="active")
    fake.seed_listing(LISTING_DEAD_CANVAS, state="active")
    cap = ListingDeactivate(fake)
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


# --- 9. SEO edit / reprice still cannot touch state -------------------------


def test_seo_edit_and_reprice_updates_never_carry_a_state_field():
    """EtsyListingUpdate (what SEO edit / reprice send via update_listing)
    has no `state` field at all -- only listing.deactivate's dedicated
    update_listing_state method can ever flip state."""
    assert "state" not in EtsyListingUpdate.model_fields
    for cap_cls in (ListingReprice, ListingSeoEdit):
        assert not hasattr(cap_cls, "update_listing_state")
