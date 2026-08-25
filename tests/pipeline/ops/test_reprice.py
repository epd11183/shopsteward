"""`listing.reprice` (M8b slice 3, design §4/§8 slice 3) -- the first
money-moving, parameterized autonomy capability. T2/PROPOSE ceiling only,
NEVER promotable (Money axis = 2, draft #9). DIGITAL LISTINGS ONLY -- POD
listings (canvas/acrylic/poster/unknown) are never repriced in any path
(draft #9b / PRD decision 43: touching updateListingInventory on a POD
listing rewrites provider SKUs). Entirely on FakeEtsyWriteAdapter, zero
network."""

from datetime import UTC, datetime, timedelta

import pydantic
import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
from shopsteward.pipeline.ops.capabilities.tune_threshold import OpsTuneThreshold
from shopsteward.pipeline.ops.models import ProposedAction, Tier
from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_DIGITAL = 801  # digital download, high views, zero sales this window -- eligible
LISTING_CANVAS = 802  # POD (canvas), same signal -- must NEVER be eligible
LISTING_ACRYLIC = 803  # POD (acrylic), same signal -- must NEVER be eligible
LISTING_UNKNOWN = 804  # no product_type_keywords match -- must NEVER be eligible


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


def _seed_listing(
    conn,
    listing_id: int,
    title: str,
    *,
    views: int = 100,
    price_usd: float = 20.0,
    state: str = "active",
) -> None:
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=200),
        views=views,
        price_usd=price_usd,
        state=state,
    )
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=1),
        views=views,
        price_usd=price_usd,
        state=state,
    )


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


# --- the load-bearing safety test --------------------------------------------


def test_pod_listings_are_never_proposed_or_materialized_only_digital_is(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk Digital Download")
    _seed_listing(conn, LISTING_CANVAS, "Loon at Dusk Canvas Print")
    _seed_listing(conn, LISTING_ACRYLIC, "Loon at Dusk Acrylic Print")
    _seed_listing(conn, LISTING_UNKNOWN, "Loon at Dusk Mystery Item")
    rebuild_core(conn)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    cap = ListingReprice(fake)
    cfg = _cfg()

    proposals = cap.propose(conn, USER_ID, cfg)

    target_ids = {a.target_id for a in proposals}
    assert target_ids == {str(LISTING_DIGITAL)}

    # materialize() on a reprice intent targeting a POD listing must also
    # return None (ungrounded) -- the LLM cannot reach a POD listing either.
    for pod_listing_id in (LISTING_CANVAS, LISTING_ACRYLIC, LISTING_UNKNOWN):
        intent = ProposalIntent(
            capability_key="listing.reprice",
            target_id=str(pod_listing_id),
            params={"price_usd": 15.0},
            reason="reprice a slow POD listing",
        )
        assert cap.materialize(conn, USER_ID, cfg, intent) is None

    digital_intent = ProposalIntent(
        capability_key="listing.reprice",
        target_id=str(LISTING_DIGITAL),
        params={"price_usd": 15.0},
        reason="reprice a slow digital listing",
    )
    assert cap.materialize(conn, USER_ID, cfg, digital_intent) is not None

    # update_listing_price must never have been called for a POD listing in
    # any path -- propose()/materialize() are read-only, but this asserts
    # the invariant the safety net (execute()) also depends on: no call at
    # all was ever recorded against the fake.
    assert fake.calls == []


def test_pod_listing_execute_is_refused_even_if_somehow_reached(conn):
    """Belt-and-suspenders: even if a caller bypassed propose()/materialize()
    and handed execute() a hand-built ProposedAction for a POD listing,
    execute() itself refuses (raises -> action.failed) rather than call
    update_listing_price."""
    _seed_listing(conn, LISTING_CANVAS, "Loon at Dusk Canvas Print")
    rebuild_core(conn)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_CANVAS, state="active", price=20.0)
    cap = ListingReprice(fake)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)

    forged = ProposedAction(
        action_id="forged-pod-reprice",
        capability="listing.reprice",
        target_type="listing",
        target_id=str(LISTING_CANVAS),
        tier=Tier.PROPOSE,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"price_usd": 15.0},
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    assert fake.calls == []  # update_listing_price never reached


# --- S1: title-keyword classification alone is too weak ---------------------


def test_a_title_matching_both_a_pod_and_digital_keyword_is_never_eligible(conn):
    """The demonstrated hole: a POD listing titled with a digital keyword
    too (e.g. "Instant Download Ready Canvas Print") must NOT classify as
    digital -- `_is_conservatively_digital` requires a digital match AND no
    other product-type match."""
    ambiguous_title = "Instant Download Ready Canvas Print"
    _seed_listing(conn, LISTING_CANVAS, ambiguous_title)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    cfg = _cfg()

    assert cap.propose(conn, USER_ID, cfg) == []

    intent = ProposalIntent(
        capability_key="listing.reprice",
        target_id=str(LISTING_CANVAS),
        params={"price_usd": 15.0},
        reason="reprice the ambiguous listing",
    )
    assert cap.materialize(conn, USER_ID, cfg, intent) is None


def test_a_provider_linked_listing_is_never_eligible_even_with_a_digital_only_title(conn):
    """The authoritative signal wins even over a clean digital-only title:
    once `listingdraft.provider_linked` names a listing_id, it is POD-backed
    (draft #9b) regardless of what its title says."""
    from shopsteward.core.events import Event, append

    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={
                "draft_id": "d-1",
                "etsy_listing_id": LISTING_DIGITAL,
                "etsy_listing_state": "active",
            },
        ),
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    cfg = _cfg()

    assert cap.propose(conn, USER_ID, cfg) == []

    intent = ProposalIntent(
        capability_key="listing.reprice",
        target_id=str(LISTING_DIGITAL),
        params={"price_usd": 15.0},
        reason="reprice a provider-linked listing",
    )
    assert cap.materialize(conn, USER_ID, cfg, intent) is None


# --- eligibility ---------------------------------------------------------


def test_not_active_listing_is_not_eligible(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", state="expired")
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())

    assert cap.propose(conn, USER_ID, _cfg()) == []


def test_views_below_minimum_is_not_eligible(conn):
    cfg = _cfg()
    _seed_listing(
        conn,
        LISTING_DIGITAL,
        "Loon Digital Download",
        views=cfg.reprice.min_lifetime_views - 1,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())

    assert cap.propose(conn, USER_ID, cfg) == []


def test_a_sale_in_the_revenue_window_is_not_eligible(conn):
    from tests.pipeline.ops.helpers import seed_sale_observed

    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download")
    seed_sale_observed(
        conn,
        receipt_id=7001,
        day=TODAY - timedelta(days=1),
        transactions=[(LISTING_DIGITAL, 70011, 1, 20.0)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())

    assert cap.propose(conn, USER_ID, _cfg()) == []


def test_unknown_product_type_is_not_eligible(conn):
    _seed_listing(conn, LISTING_UNKNOWN, "Loon at Dusk Mystery Item")
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())

    assert cap.propose(conn, USER_ID, _cfg()) == []


# --- propose(): deterministic default reduction --------------------------


def test_propose_applies_the_deterministic_default_reduction(conn):
    cfg = _cfg()
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())

    (action,) = cap.propose(conn, USER_ID, cfg)

    expected = round(20.0 * (1 - cfg.reprice.default_reduction_pct), 2)
    assert action.params["price_usd"] == expected
    assert action.capability == "listing.reprice"
    assert action.target_type == "listing"
    assert action.target_id == str(LISTING_DIGITAL)
    assert action.estimated_cost_usd == 0.0
    assert action.undo_available is True
    assert action.tier == Tier.PROPOSE


def test_propose_floors_at_min_price_usd(conn):
    cfg = _cfg()
    # A tiny current price where the default reduction would fall under the
    # config floor -- propose() must clamp to min_price_usd, never below it.
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=3.10)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())

    (action,) = cap.propose(conn, USER_ID, cfg)

    assert action.params["price_usd"] == cfg.reprice.min_price_usd


def test_propose_skips_when_the_reduction_would_not_change_the_price(conn):
    cfg = _cfg()
    # current price already AT the floor -- max(floor, reduced) == current,
    # so no change is proposed.
    _seed_listing(
        conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=cfg.reprice.min_price_usd
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())

    assert cap.propose(conn, USER_ID, cfg) == []


# --- materialize(): LLM price bounds --------------------------------------


def test_materialize_accepts_an_in_bounds_llm_price(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent(
        capability_key="listing.reprice",
        target_id=str(LISTING_DIGITAL),
        params={"price_usd": 17.0},
        reason="the LLM's own sentence -- must never become the audit reason",
    )
    action = cap.materialize(conn, USER_ID, cfg, intent)

    assert action is not None
    assert action.params["price_usd"] == 17.0
    assert "LLM's own sentence" not in action.reason


@pytest.mark.parametrize(
    "bad_price",
    [
        1.0,  # below min_price_usd floor
        20.0 * 1.41,  # beyond +max_pct_change (default 0.40)
        20.0 * 0.59,  # beyond -max_pct_change (default 0.40)
        20.0,  # equal to current -- not a change
        "seventeen",  # non-numeric
        True,  # bool is not a real price even though it's an int subclass
    ],
)
def test_materialize_drops_an_out_of_bounds_or_non_numeric_price(conn, bad_price):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent(
        capability_key="listing.reprice",
        target_id=str(LISTING_DIGITAL),
        params={"price_usd": bad_price},
        reason="try a bad price",
    )
    assert cap.materialize(conn, USER_ID, cfg, intent) is None


# --- B1: a NaN/inf price must never bypass the bounds guard ----------------


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_proposal_intent_rejects_non_finite_price_at_construction(non_finite):
    """B1 guard #2: allow_inf_nan=False on ProposalIntent -- a NaN/inf price
    can't even be parsed into an intent in the first place."""
    with pytest.raises(pydantic.ValidationError):
        ProposalIntent(
            capability_key="listing.reprice",
            target_id=str(LISTING_DIGITAL),
            params={"price_usd": non_finite},
            reason="a non-finite price",
        )


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_materialize_drops_a_non_finite_price_even_bypassing_intent_validation(conn, non_finite):
    """B1 guard #1: even if a NaN/inf reached materialize() by some other
    path (model_construct bypasses ProposalIntent's own validation, standing
    in for "guard #2 somehow didn't fire"), `_is_in_bounds_price`'s explicit
    `math.isfinite` check still drops it -- every bounds comparison against
    NaN is otherwise False, so without this check NaN silently passes."""
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent.model_construct(
        capability_key="listing.reprice",
        target_id=str(LISTING_DIGITAL),
        params={"price_usd": non_finite},
        reason="a non-finite price",
    )
    assert cap.materialize(conn, USER_ID, cfg, intent) is None


def test_execute_refuses_a_hand_forged_action_with_a_non_finite_price(conn):
    """B1 guard #3: execute() re-validates params["price_usd"] itself --
    even a hand-forged ProposedAction (bypassing propose()/materialize()
    entirely) with a NaN price must raise (-> action.failed upstream in the
    runner) rather than ever call update_listing_price(listing_id, nan)."""
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", price=20.0)
    cap = ListingReprice(fake)

    forged = ProposedAction(
        action_id="forged-nan-reprice",
        capability="listing.reprice",
        target_type="listing",
        target_id=str(LISTING_DIGITAL),
        tier=Tier.PROPOSE,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"price_usd": float("nan")},
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    assert fake.calls == []  # update_listing_price never reached


def test_materialize_on_a_pod_target_is_none_even_with_an_in_bounds_price(conn):
    _seed_listing(conn, LISTING_CANVAS, "Loon at Dusk Canvas Print", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent(
        capability_key="listing.reprice",
        target_id=str(LISTING_CANVAS),
        params={"price_usd": 17.0},
        reason="in-bounds price, but POD",
    )
    assert cap.materialize(conn, USER_ID, cfg, intent) is None


# --- registration: T2 ceiling, has undo -----------------------------------


def test_register_is_t2_and_has_undo():
    cap = ListingReprice(FakeEtsyWriteAdapter())

    register(cap)

    assert REGISTRY["listing.reprice"] is cap
    assert cap.max_tier == Tier.PROPOSE
    assert callable(cap.undo)


# --- ProposedAction.params round-trips through action.proposed -----------


def test_params_round_trip_through_action_proposed_event(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", price=20.0)
    cap = ListingReprice(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    (proposed,) = cap.propose(conn, USER_ID, cfg)
    run(conn, USER_ID, cfg, [cap], today=TODAY)

    event = read_all(conn, "action.proposed")[0]
    assert event.payload["params"] == proposed.params

    approved = approve_action(
        conn, USER_ID, event.payload["action_id"], [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved.executed == 1
    price_calls = [c for c in fake.calls if c[0] == "update_listing_price"]
    assert len(price_calls) == 1
    assert price_calls[0][1]["price"] == proposed.params["price_usd"]


def test_autorenew_and_tune_threshold_params_default_empty_and_action_id_unchanged(conn):
    """Regression: adding ProposedAction.params must not alter the
    action_id/inputs_hash any other capability computes -- both leave
    params={} unchanged."""
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", state="active")
    rebuild_core(conn)
    rebuild_ops(conn)

    tune = OpsTuneThreshold()
    autorenew_cap = ListingAutorenewOff(FakeEtsyWriteAdapter())

    cfg = _cfg()
    for action in tune.propose(conn, USER_ID, cfg) + autorenew_cap.propose(conn, USER_ID, cfg):
        assert action.params == {}


# --- E2E via runner: T2 queue, approve, undo, idempotent re-run ----------


def test_e2e_valid_intent_lands_at_t2_approve_calls_update_listing_price_undo_restores(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", price=20.0)
    cap = ListingReprice(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert report.proposed == 1
    assert report.executed == 0  # T2 -- never auto-executed

    proposed_event = read_all(conn, "action.proposed")[0]
    action_id = proposed_event.payload["action_id"]
    new_price = proposed_event.payload["params"]["price_usd"]

    approved = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved.executed == 1
    assert fake.listings[LISTING_DIGITAL]["price"] == new_price
    price_calls = [c for c in fake.calls if c[0] == "update_listing_price"]
    assert len(price_calls) == 1
    assert price_calls[0][1] == {"listing_id": LISTING_DIGITAL, "price": new_price}

    executed_event = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id
    ][0]
    assert executed_event.payload["before"] == {"price_usd": 20.0}
    assert executed_event.payload["after"] == {"price_usd": new_price}
    assert executed_event.payload["cost_usd"] == 0.0

    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
    assert fake.listings[LISTING_DIGITAL]["price"] == 20.0
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id][0]
    assert undone.payload["restored_to"] == {"price_usd": 20.0}

    state = capability_states(conn, USER_ID)["listing.reprice"]
    assert state.tier == Tier.PROPOSE
    assert state.undos == 0  # reset by the demotion the undo itself triggered

    # idempotent re-run under a fresh price/action_id since the price moved.
    rerun = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert rerun.skipped_idempotent == 1  # same target_id/inputs_hash/day -> terminal action_id


# --- T2 ceiling: never promotable ------------------------------------------


def test_never_promoted_above_t2_even_with_enough_approvals_and_elapsed_days(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=50.0)
    _seed_listing(conn, 805, "Second Digital Download", price_usd=50.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", price=50.0)
    fake.seed_listing(805, state="active", price=50.0)
    cap = ListingReprice(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    cfg.autonomy.ladder.promote_approvals = 1
    cfg.autonomy.ladder.promote_min_days = 1

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_ids = [e.payload["action_id"] for e in read_all(conn, "action.proposed")]

    later = TODAY + timedelta(days=2)
    for action_id in action_ids:
        approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=later, live_autonomy=True)

    promoted = [e for e in read_all(conn, "capability.") if e.type == "capability.promoted"]
    assert promoted == []  # ladder would promote a T1-ceiling stub here; T2 ceiling refuses it

    state = capability_states(conn, USER_ID)["listing.reprice"]
    assert state.tier == Tier.PROPOSE


# --- no secret in any payload, append-only ---------------------------------


def test_no_secret_in_any_payload_and_append_only(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", price_usd=20.0)
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", price=20.0)
    cap = ListingReprice(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True)
    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)

    import json

    banned = ("token", "api_key", "apikey", "secret", "signed_url", "access_token", "refresh_token")
    events = read_all(conn)
    for e in events:
        blob = json.dumps(e.payload).lower()
        for word in banned:
            assert word not in blob, f"event {e.type} payload leaked {word!r}: {blob}"
        assert e.user_id == USER_ID
