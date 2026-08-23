"""`listing.renew` (M8b slice 4c) -- bring back an expired listing with real
sales history (state expired->active). `PATCH .../listings/{id}
state=active` IS the renewal mechanism (there is no separate renewListing
endpoint) and Etsy silently charges ~$0.20 for it -- this file confirms that
cost flows through to ExecutionResult/the governor's monthly spend cap
(policy_verified = True since E15 landed), and that the policy_unverified
refusal path itself still holds for any capability that isn't verified.
Entirely on FakeEtsyWriteAdapter, zero network."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.renew import ListingRenew
from shopsteward.pipeline.ops.models import ProposedAction, RefusalReason, Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_EXPIRED_WITH_SALE = 921  # expired, 1+ real sale, quantity>=1 -- SHOULD be proposed
LISTING_EXPIRED_NO_SALE = 922  # expired, 0 sales -- should NOT
LISTING_ACTIVE_WITH_SALE = 923  # active (not expired), 1+ sale -- should NOT
LISTING_EXPIRED_OUT_OF_STOCK = 924  # expired, 1+ sale, quantity=0 -- should NOT


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


def _seed_scenario(conn) -> None:
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_EXPIRED_WITH_SALE,
        title="Loon at Dusk Canvas Print",
        day=TODAY - timedelta(days=1),
        views=0,
        state="expired",
        quantity=5,
    )
    seed_sale_observed(
        conn,
        receipt_id=8001,
        day=TODAY - timedelta(days=100),
        transactions=[(LISTING_EXPIRED_WITH_SALE, 80011, 1, 40.00)],
    )

    seed_listing_observed_on(
        conn,
        listing_id=LISTING_EXPIRED_NO_SALE,
        title="Never Sold Print",
        day=TODAY - timedelta(days=1),
        views=0,
        state="expired",
        quantity=5,
    )

    seed_listing_observed_on(
        conn,
        listing_id=LISTING_ACTIVE_WITH_SALE,
        title="Still Active Print",
        day=TODAY - timedelta(days=1),
        views=0,
        state="active",
        quantity=5,
    )
    seed_sale_observed(
        conn,
        receipt_id=8002,
        day=TODAY - timedelta(days=100),
        transactions=[(LISTING_ACTIVE_WITH_SALE, 80021, 1, 40.00)],
    )

    seed_listing_observed_on(
        conn,
        listing_id=LISTING_EXPIRED_OUT_OF_STOCK,
        title="Sold Out Print",
        day=TODAY - timedelta(days=1),
        views=0,
        state="expired",
        quantity=0,
    )
    seed_sale_observed(
        conn,
        receipt_id=8003,
        day=TODAY - timedelta(days=100),
        transactions=[(LISTING_EXPIRED_OUT_OF_STOCK, 80031, 1, 40.00)],
    )

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


# --- 1. eligibility -----------------------------------------------------------


def test_propose_yields_exactly_the_expired_with_sale_and_stock(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    cap = ListingRenew(fake)

    actions = cap.propose(conn, USER_ID, _cfg())

    target_ids = {a.target_id for a in actions}
    assert target_ids == {str(LISTING_EXPIRED_WITH_SALE)}
    action = actions[0]
    assert action.capability == "listing.renew"
    assert action.target_type == "listing"
    assert action.undo_available is True
    assert "0.20" in action.reason
    assert "lifetime sale" in action.reason


# --- 2. cost flows through ----------------------------------------------------


def test_estimated_cost_usd_is_the_configured_listing_fee(conn):
    _seed_scenario(conn)
    cap = ListingRenew(FakeEtsyWriteAdapter())
    cfg = _cfg()

    action = cap.propose(conn, USER_ID, cfg)[0]

    assert action.estimated_cost_usd == cfg.renew.listing_fee_usd == 0.20


# --- 3. execute/undo -----------------------------------------------------------


def test_execute_calls_update_listing_state_active_and_returns_honest_before(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_EXPIRED_WITH_SALE, state="expired", quantity=5)
    cap = ListingRenew(fake)
    action = cap.propose(conn, USER_ID, _cfg())[0]

    result = cap.execute(conn, USER_ID, action)

    # before must NOT claim undo can restore "expired" -- there is no Etsy
    # API for that and the fee is non-refundable regardless (F1 fix).
    assert result.before != {"state": "expired"}
    assert "expired" in result.before["state"]
    assert "inactive" in result.before["state"]
    assert "non-refundable" in result.before["state"]
    assert result.after == {"state": "active"}
    assert result.cost_usd == 0.20
    expected_call = (
        "update_listing_state",
        {"listing_id": LISTING_EXPIRED_WITH_SALE, "state": "active"},
    )
    assert expected_call in fake.calls


def test_execute_refuses_a_hand_forged_action_no_longer_eligible(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_EXPIRED_NO_SALE, state="expired", quantity=5)
    cap = ListingRenew(fake)

    forged = ProposedAction(
        action_id="forged-renew",
        capability="listing.renew",
        target_type="listing",
        target_id=str(LISTING_EXPIRED_NO_SALE),
        tier=Tier.NOTIFY,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.20,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    state_calls = [c for c in fake.calls if c[0] == "update_listing_state"]
    assert state_calls == []


def test_undo_calls_update_listing_state_inactive(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_EXPIRED_WITH_SALE, state="active")  # post-execute state
    cap = ListingRenew(fake)
    action = cap.propose(conn, USER_ID, _cfg())[0]

    cap.undo(conn, USER_ID, action)

    expected_call = (
        "update_listing_state",
        {"listing_id": LISTING_EXPIRED_WITH_SALE, "state": "inactive"},
    )
    assert expected_call in fake.calls


def test_undo_event_restored_to_is_honest_not_a_false_expired_claim(conn, monkeypatch):
    """F1: action.undone's `restored_to` (what cli.py's `ops undo` prints
    verbatim) must never claim undo restored "expired" -- undo() only ever
    calls update_listing_state(..., "inactive"). policy_verified is
    monkeypatched True here (E15 is a separate, unrelated task) purely to
    drive the full run -> approve -> undo pipeline end to end."""
    _seed_scenario(conn)
    monkeypatch.setattr(ListingRenew, "policy_verified", True)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_EXPIRED_WITH_SALE, state="expired", quantity=5)
    cap = ListingRenew(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0, monthly_spend_cap_usd=20.00)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = next(
        e.payload["action_id"]
        for e in read_all(conn, "action.proposed")
        if e.payload["target_id"] == str(LISTING_EXPIRED_WITH_SALE)
    )
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    undo_action(conn, USER_ID, action_id, [cap])

    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id]
    assert len(undone) == 1
    restored_to = undone[0].payload["restored_to"]
    assert restored_to != {"state": "expired"}
    assert "non-refundable" in restored_to["state"]
    assert "inactive" in restored_to["state"]


# --- 4. registration: no undo -> can't register above T2 (sanity) -----------


def test_register_succeeds_capability_has_undo_and_t2_ceiling():
    cap = ListingRenew(FakeEtsyWriteAdapter())

    register(cap)

    assert REGISTRY["listing.renew"] is cap
    assert cap.max_tier == Tier.NOTIFY
    assert callable(cap.undo)


# --- 5. policy_unverified refuses every proposal, end to end ----------------


def test_policy_unverified_refuses_the_proposal_via_the_full_run_pipeline(conn, monkeypatch):
    """E15 has since landed (policy_verified is True by default), but the
    policy_unverified refusal path itself must still hold for any capability
    that isn't verified -- prove it by forcing this one back to False."""
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_EXPIRED_WITH_SALE, state="expired", quantity=5)
    monkeypatch.setattr(ListingRenew, "policy_verified", False)
    cap = ListingRenew(fake)
    assert cap.policy_verified is False
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0, monthly_spend_cap_usd=20.00)

    run(conn, USER_ID, cfg, [cap], today=TODAY)

    refused = [e for e in read_all(conn, "action.refused")]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == RefusalReason.POLICY_UNVERIFIED.value
    # never actually renewed -- refused before execute() ever runs.
    assert [c for c in fake.calls if c[0] == "update_listing_state"] == []


# --- 6. cost genuinely reaches the governor's monthly spend cap --------------

LISTING_EXPIRED_WITH_SALE_2 = 925  # a second expired+sale+stock listing


def test_renew_fee_reaches_month_spend_and_trips_budget_on_the_second(conn, monkeypatch):
    """F5.1: $0.20/renewal must genuinely reach governor.month_spend() (not
    just sit in ExecutionResult unread) -- prove it by exhausting a cap sized
    for exactly one renewal and watching the second get refused BUDGET.
    policy_verified is monkeypatched True (E15 is a separate, unrelated
    task) purely to exercise the real run/approve/govern accounting path."""
    _seed_scenario(conn)
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_EXPIRED_WITH_SALE_2,
        title="Second Expired Print",
        day=TODAY - timedelta(days=1),
        views=0,
        state="expired",
        quantity=5,
    )
    seed_sale_observed(
        conn,
        receipt_id=8004,
        day=TODAY - timedelta(days=100),
        transactions=[(LISTING_EXPIRED_WITH_SALE_2, 80041, 1, 40.00)],
    )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    monkeypatch.setattr(ListingRenew, "policy_verified", True)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_EXPIRED_WITH_SALE, state="expired", quantity=5)
    fake.seed_listing(LISTING_EXPIRED_WITH_SALE_2, state="expired", quantity=5)
    cap = ListingRenew(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0, monthly_spend_cap_usd=0.20)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    proposed = {
        e.payload["target_id"]: e.payload["action_id"] for e in read_all(conn, "action.proposed")
    }
    action_id_1 = proposed[str(LISTING_EXPIRED_WITH_SALE)]
    action_id_2 = proposed[str(LISTING_EXPIRED_WITH_SALE_2)]

    first = approve_action(conn, USER_ID, action_id_1, [cap], cfg=cfg, today=TODAY)
    assert first.executed == 1

    second = approve_action(conn, USER_ID, action_id_2, [cap], cfg=cfg, today=TODAY)
    assert second.refused == 1
    refused = [e for e in read_all(conn, "action.refused") if e.payload["action_id"] == action_id_2]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == RefusalReason.BUDGET.value
    # the second listing was never actually renewed.
    state_calls = [c for c in fake.calls if c[0] == "update_listing_state"]
    assert (LISTING_EXPIRED_WITH_SALE_2, "active") not in {
        (c[1]["listing_id"], c[1]["state"]) for c in state_calls
    }


# --- 7. materialize() -- the planner-safety grounding hook --------------------


def test_materialize_shares_candidates_with_propose(conn):
    _seed_scenario(conn)
    cap = ListingRenew(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent(
        capability_key="listing.renew",
        target_id=str(LISTING_EXPIRED_WITH_SALE),
        params={},
        reason="expired listing with real sales history, renew it",
    )
    action = cap.materialize(conn, USER_ID, cfg, intent)
    assert action is not None
    assert action.target_id == str(LISTING_EXPIRED_WITH_SALE)
    assert action.capability == "listing.renew"

    for hallucinated in (str(LISTING_EXPIRED_NO_SALE), "999999"):
        bad_intent = ProposalIntent(
            capability_key="listing.renew",
            target_id=hallucinated,
            params={},
            reason="hallucinated target",
        )
        assert cap.materialize(conn, USER_ID, cfg, bad_intent) is None
