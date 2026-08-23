"""`listing.seo_edit` (M8b slice 4a, design §4/§8 slice 4; widened slice --
description + expired-with-sales) -- Claude rewrites a listing's
title/tags/description, the operator approves each edit. T2/PROPOSE ceiling
only, NEVER promotable (draft #10). Planner-only: propose() always []. Both
digital AND POD listings are eligible (update_listing never touches SKUs).
Eligible listings are either active/viewed-but-not-sold OR expired with real
historical sales (the same bar `listing.renew` uses,
`cfg.renew.min_lifetime_sales`). description edits require a non-None
baseline (`current_description`) to keep undo always truthful. Entirely on
FakeEtsyWriteAdapter, zero network."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.seo_edit import ListingSeoEdit
from shopsteward.pipeline.ops.models import ProposedAction, Tier
from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_DIGITAL = 901  # digital download, high views, zero sales this window -- eligible
LISTING_CANVAS = 902  # POD (canvas), same signal -- ALSO eligible (SEO never touches SKUs)


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
    state: str = "active",
    tags: list[str] | None = None,
    description: str | None = None,
    quantity: int = 999,
) -> None:
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=200),
        views=views,
        state=state,
        tags=tags,
        description=description,
        quantity=quantity,
    )
    seed_listing_observed_on(
        conn,
        listing_id=listing_id,
        title=title,
        day=TODAY - timedelta(days=1),
        views=views,
        state=state,
        tags=tags,
        description=description,
        quantity=quantity,
    )


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def _intent(target_id: str, **params) -> ProposalIntent:
    return ProposalIntent(
        capability_key="listing.seo_edit",
        target_id=target_id,
        params=params,
        reason="the LLM's own sentence -- must never become the audit reason",
    )


# --- propose(): planner-only, always empty ----------------------------------


def test_propose_always_returns_empty(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk Digital Download", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.propose(conn, USER_ID, _cfg()) == []


# --- eligibility: both digital AND POD; not active/low views/sold excluded --


def test_both_digital_and_pod_listing_are_eligible(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk Digital Download", tags=["loon"])
    _seed_listing(conn, LISTING_CANVAS, "Loon at Dusk Canvas Print", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    for listing_id in (LISTING_DIGITAL, LISTING_CANVAS):
        action = cap.materialize(conn, USER_ID, cfg, _intent(str(listing_id), title="New Title"))
        assert action is not None
        assert action.target_id == str(listing_id)


def test_not_active_listing_is_not_eligible(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", state="expired")
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_DIGITAL), title="X")) is None


def test_views_below_minimum_is_not_eligible(conn):
    cfg = _cfg()
    _seed_listing(
        conn, LISTING_DIGITAL, "Loon Digital Download", views=cfg.seo_edit.min_lifetime_views - 1
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), title="X")) is None


def test_a_sale_in_the_revenue_window_is_not_eligible(conn):
    from tests.pipeline.ops.helpers import seed_sale_observed

    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download")
    seed_sale_observed(
        conn,
        receipt_id=8001,
        day=TODAY - timedelta(days=1),
        transactions=[(LISTING_DIGITAL, 80011, 1, 20.0)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_DIGITAL), title="X")) is None


# --- eligibility: expired listings with genuine historical sales -----------

LISTING_EXPIRED_WITH_SALE = 931  # expired, >= cfg.renew.min_lifetime_sales real sales -- eligible
LISTING_EXPIRED_NO_SALE = 932  # expired, 0 sales -- NOT eligible


def test_expired_listing_with_enough_lifetime_sales_is_eligible(conn):
    cfg = _cfg()
    _seed_listing(
        conn, LISTING_EXPIRED_WITH_SALE, "Old Print", views=0, state="expired", quantity=5
    )
    seed_sale_observed(
        conn,
        receipt_id=8101,
        day=TODAY - timedelta(days=100),
        transactions=[(LISTING_EXPIRED_WITH_SALE, 81011, 1, 40.0)] * cfg.renew.min_lifetime_sales,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_EXPIRED_WITH_SALE), title="Old Print Restyled")
    )
    assert action is not None
    assert action.target_id == str(LISTING_EXPIRED_WITH_SALE)


def test_expired_listing_with_zero_sales_is_not_eligible(conn):
    _seed_listing(conn, LISTING_EXPIRED_NO_SALE, "Never Sold", views=0, state="expired", quantity=5)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert (
        cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_EXPIRED_NO_SALE), title="X"))
        is None
    )


def test_active_listing_is_not_double_counted_by_the_expired_branch(conn):
    """An active, already-eligible listing must appear exactly once (via the
    active branch), never duplicated by the expired-with-sales branch."""
    cfg = _cfg()
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk Digital Download", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)

    from shopsteward.pipeline.ops.capabilities.seo_edit import _eligible

    targets = _eligible(conn, USER_ID, cfg)
    assert list(targets).count(str(LISTING_DIGITAL)) == 1


def test_expired_listing_views_never_gate_eligibility(conn):
    """Etsy returns views:0 for every expired listing regardless of real
    sales history -- eligibility must never be gated on it."""
    cfg = _cfg()
    _seed_listing(
        conn, LISTING_EXPIRED_WITH_SALE, "Old Print", views=0, state="expired", quantity=5
    )
    seed_sale_observed(
        conn,
        receipt_id=8102,
        day=TODAY - timedelta(days=100),
        transactions=[(LISTING_EXPIRED_WITH_SALE, 81021, 1, 40.0)] * cfg.renew.min_lifetime_sales,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert (
        cap.materialize(
            conn, USER_ID, cfg, _intent(str(LISTING_EXPIRED_WITH_SALE), title="Restyled")
        )
        is not None
    )


def test_expired_listing_with_only_fixture_polluted_sales_is_not_eligible(
    conn, tmp_path, monkeypatch
):
    """A dev DB polluted by an old `--fixtures` sync (tiny synthetic ids)
    that predates the real shop's own `etsy.shop.observed` anchor must not
    count toward `cfg.renew.min_lifetime_sales` -- mirrors
    read_live_observed()'s own guard test in tests/core/test_sync.py."""
    import shopsteward.adapters.etsy.auth as auth_mod
    from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore

    monkeypatch.setattr(auth_mod, "etsy_tokens_path", lambda: tmp_path / "etsy_tokens.json")

    listing_id = 933
    # fixture-era shop + sale -- predates the real shop.observed anchor.
    append(conn, Event(user_id=USER_ID, type="etsy.shop.observed", payload={"shop_id": 100001}))
    seed_sale_observed(
        conn,
        receipt_id=8103,
        day=TODAY - timedelta(days=200),
        transactions=[(listing_id, 81031, 1, 40.0)],
    )
    # real shop anchor, after the fixture rows -- no real sale follows it.
    append(conn, Event(user_id=USER_ID, type="etsy.shop.observed", payload={"shop_id": 52644245}))
    _seed_listing(conn, listing_id, "Old Print", views=0, state="expired", quantity=5)

    store = EtsyTokenStore()
    store.save(
        EtsyTokens(
            access_token="t",
            access_expires_at=9999999999.0,
            refresh_token="r",
            shop_id=52644245,
            etsy_user_id=1,
            scopes=["shops_r"],
        )
    )

    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(listing_id), title="X")) is None


# --- materialize(): structural validation + diff ----------------------------


def test_materialize_accepts_a_valid_title_and_tags_change(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon", "bird"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        _intent(str(LISTING_DIGITAL), title="Loon at Dusk Fine Art Print", tags=["loon", "sunset"]),
    )

    assert action is not None
    assert action.params == {"title": "Loon at Dusk Fine Art Print", "tags": ["loon", "sunset"]}
    assert "LLM's own sentence" not in action.reason
    assert action.tier == Tier.PROPOSE
    assert action.estimated_cost_usd == 0.0
    assert action.undo_available is True


def test_materialize_only_carries_the_field_that_actually_changed(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = _intent(str(LISTING_DIGITAL), title="Loon at Dusk", tags=["loon", "new"])
    action = cap.materialize(conn, USER_ID, cfg, intent)

    assert action is not None
    assert action.params == {"tags": ["loon", "new"]}  # title unchanged -- never carried


def test_materialize_drops_description_with_no_baseline_to_diff_or_restore(conn):
    """current_description is None (this listing was never observed with a
    description, e.g. synced before the field existed) -- a proposed
    description must be silently dropped, never validated or sent, and must
    never itself count as a change. This keeps undo_available=True always
    truthful: there is never a kept description edit with no baseline."""
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])  # no description -> None
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    # description-only intent: nothing else changes -- must be dropped as a no-op.
    assert (
        cap.materialize(
            conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), description="a new description")
        )
        is None
    )

    # description alongside a real title change: description must not appear in params.
    action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        _intent(str(LISTING_DIGITAL), title="New Title", description="a new description"),
    )
    assert action is not None
    assert action.params == {"title": "New Title"}
    assert "description" not in action.params


def test_materialize_keeps_a_description_change_with_a_real_baseline(conn):
    _seed_listing(
        conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"], description="Original description."
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), description="A brand new description.")
    )
    assert action is not None
    assert action.params == {"description": "A brand new description."}


def test_materialize_drops_a_description_identical_to_the_current_baseline(conn):
    _seed_listing(
        conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"], description="Original description."
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    assert (
        cap.materialize(
            conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), description="Original description.")
        )
        is None
    )


def test_materialize_drops_an_over_length_description_never_truncates(conn):
    _seed_listing(
        conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"], description="Original description."
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), description="x" * 5001)
    )
    assert action is None  # dropped whole key, never truncated -- also a no-op with nothing else

    # over-length description alongside a real title change: title survives independently.
    action2 = cap.materialize(
        conn,
        USER_ID,
        cfg,
        _intent(str(LISTING_DIGITAL), title="New Title", description="x" * 5001),
    )
    assert action2 is not None
    assert action2.params == {"title": "New Title"}


@pytest.mark.parametrize(
    "params",
    [
        {"title": "x" * 141},  # title > 140
        {"title": ""},  # empty title
        {"title": 12345},  # non-str title
        {"tags": ["a" * 21]},  # a tag > 20 chars
        {"tags": [str(i) for i in range(14)]},  # > 13 tags
        {"tags": []},  # empty tags list
        {"tags": ["ok", ""]},  # an empty tag in the list
        {"tags": "not-a-list"},  # non-list tags
        {"tags": ["black, white", "red"]},  # a tag containing a comma
        {"title": "Loon at Dusk", "tags": ["loon"]},  # matches current -- no-op
    ],
)
def test_materialize_drops_structurally_invalid_or_unchanged_params(conn, params):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    assert cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), **params)) is None


def test_materialize_drops_a_non_str_tag_even_bypassing_intent_validation(conn):
    """ProposalIntent's own `list[str]` typing already rejects a non-str tag
    at construction (Pydantic) -- this proves materialize()'s own
    `_validate_params` also guards it directly (model_construct bypasses
    ProposalIntent's validation, standing in for "the pydantic guard somehow
    didn't fire"), same belt-and-suspenders shape as reprice's B1 NaN test."""
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent.model_construct(
        capability_key="listing.seo_edit",
        target_id=str(LISTING_DIGITAL),
        params={"tags": ["ok", 5]},
        reason="a non-str tag",
    )
    assert cap.materialize(conn, USER_ID, cfg, intent) is None


def test_materialize_drops_when_no_fields_present_at_all(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_DIGITAL))) is None


# --- registration: T2 ceiling, has undo -------------------------------------


def test_register_is_t2_and_has_undo():
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    register(cap)

    assert REGISTRY["listing.seo_edit"] is cap
    assert cap.max_tier == Tier.PROPOSE
    assert callable(cap.undo)


# --- params (incl. a tags LIST) round-trip through action.proposed ---------


def test_tags_list_params_round_trip_through_action_proposed_event(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), tags=["loon", "sunset", "art"])
    )
    assert action is not None
    proposals = [action]

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=proposals)
    assert report.proposed == 1
    assert report.executed == 0  # T2 -- never auto-executed

    event = read_all(conn, "action.proposed")[0]
    assert event.payload["params"] == {"tags": ["loon", "sunset", "art"]}

    approved = approve_action(
        conn, USER_ID, event.payload["action_id"], [cap], cfg=cfg, today=TODAY
    )
    assert approved.executed == 1
    update_calls = [c for c in fake.calls if c[0] == "update_listing"]
    assert len(update_calls) == 1
    assert update_calls[0][1]["fields"] == {"tags": ["loon", "sunset", "art"]}


# --- description: execute()/undo() round-trip -------------------------------


def test_e2e_description_change_execute_records_before_and_undo_restores(conn):
    _seed_listing(
        conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"], description="Original description."
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), description="A brand new description.")
    )
    assert action is not None
    assert action.params == {"description": "A brand new description."}

    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    assert approved.executed == 1
    assert fake.listings[LISTING_DIGITAL]["description"] == "A brand new description."

    update_calls = [c for c in fake.calls if c[0] == "update_listing"]
    assert len(update_calls) == 1
    assert update_calls[0][1]["fields"] == {"description": "A brand new description."}

    executed_event = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id
    ][0]
    assert executed_event.payload["before"] == {"description": "Original description."}
    assert executed_event.payload["after"] == {"description": "A brand new description."}

    undo_action(conn, USER_ID, action_id, [cap])
    assert fake.listings[LISTING_DIGITAL]["description"] == "Original description."
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id][0]
    assert undone.payload["restored_to"] == {"description": "Original description."}


# --- E2E via runner: T2 queue, approve calls update_listing with ONLY the --
# --- changed fields, undo restores, idempotent re-run -----------------------


def test_e2e_valid_intent_lands_at_t2_approve_sends_only_changed_fields_undo_restores(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), title="Loon at Dusk Fine Art Print")
    )
    assert action is not None

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    assert report.proposed == 1
    assert report.executed == 0

    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    assert approved.executed == 1
    assert fake.listings[LISTING_DIGITAL]["title"] == "Loon at Dusk Fine Art Print"
    assert fake.listings[LISTING_DIGITAL]["tags"] == ["loon"]  # untouched -- never sent

    update_calls = [c for c in fake.calls if c[0] == "update_listing"]
    assert len(update_calls) == 1
    assert update_calls[0][1] == {
        "listing_id": LISTING_DIGITAL,
        "fields": {"title": "Loon at Dusk Fine Art Print"},
    }

    executed_event = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id
    ][0]
    assert executed_event.payload["before"] == {"title": "Loon at Dusk"}
    assert executed_event.payload["after"] == {"title": "Loon at Dusk Fine Art Print"}
    assert executed_event.payload["cost_usd"] == 0.0

    undo_action(conn, USER_ID, action_id, [cap])
    assert fake.listings[LISTING_DIGITAL]["title"] == "Loon at Dusk"
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id][0]
    assert undone.payload["restored_to"] == {"title": "Loon at Dusk"}

    state = capability_states(conn, USER_ID)["listing.seo_edit"]
    assert state.tier == Tier.PROPOSE
    assert state.undos == 0  # reset by the demotion the undo itself triggered

    # idempotent re-run of the exact same materialized action.
    rerun = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    assert rerun.skipped_idempotent == 1


# --- T2 ceiling: never promotable --------------------------------------------


def test_never_promoted_above_t2_even_with_enough_approvals_and_elapsed_days(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    cfg.autonomy.ladder.promote_approvals = 1
    cfg.autonomy.ladder.promote_min_days = 1

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), title="Loon at Dusk Fine Art Print")
    )
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    later = TODAY + timedelta(days=2)
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=later)

    promoted = [e for e in read_all(conn, "capability.") if e.type == "capability.promoted"]
    assert promoted == []

    state = capability_states(conn, USER_ID)["listing.seo_edit"]
    assert state.tier == Tier.PROPOSE


# --- execute() re-validation: a hand-forged/stale action never sends junk ---


def test_execute_refuses_a_hand_forged_action_with_invalid_params(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)

    forged = ProposedAction(
        action_id="forged-seo-edit",
        capability="listing.seo_edit",
        target_type="listing",
        target_id=str(LISTING_DIGITAL),
        tier=Tier.PROPOSE,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"title": "x" * 141},  # too long
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    assert fake.calls == []  # update_listing never reached


def test_execute_refuses_when_the_listing_is_no_longer_eligible(conn):
    """The listing went inactive between materialize() and execute() (or a
    hand-forged action targets one that never qualified) -- execute() must
    raise, never call update_listing with junk."""
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", state="expired", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="expired", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)

    forged = ProposedAction(
        action_id="forged-seo-edit-inactive",
        capability="listing.seo_edit",
        target_type="listing",
        target_id=str(LISTING_DIGITAL),
        tier=Tier.PROPOSE,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"title": "New Title"},
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    assert fake.calls == []


# --- only title/tags ever sent -- never state/price/sku ---------------------


def test_update_listing_is_only_ever_called_with_title_or_tags(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        _intent(str(LISTING_DIGITAL), title="Loon at Dusk Fine Art Print", tags=["loon", "art"]),
    )
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    undo_action(conn, USER_ID, action_id, [cap])

    update_calls = [c for c in fake.calls if c[0] == "update_listing"]
    assert len(update_calls) == 2  # execute + undo
    for _name, kwargs in update_calls:
        assert set(kwargs["fields"]) <= {"title", "tags"}
        assert "state" not in kwargs["fields"]
        assert "price" not in kwargs["fields"]
        assert "sku" not in kwargs["fields"]
        assert "should_auto_renew" not in kwargs["fields"]
    # never update_listing_price/publish/delete -- only update_listing
    assert all(c[0] == "update_listing" for c in fake.calls)


# --- no secret in any payload, append-only -----------------------------------


def test_no_secret_in_any_payload_and_append_only(conn):
    _seed_listing(conn, LISTING_DIGITAL, "Loon at Dusk", tags=["loon"])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_DIGITAL, state="active", title="Loon at Dusk", tags=["loon"])
    cap = ListingSeoEdit(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), title="Loon at Dusk Fine Art Print")
    )
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
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


# --- reprice/autorenew/tune params stay unaffected by the widened type ------


def test_reprice_price_param_unaffected_by_widened_params_type(conn):
    from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice

    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", tags=[])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingReprice(FakeEtsyWriteAdapter())
    cfg = _cfg()

    (action,) = cap.propose(conn, USER_ID, cfg)
    assert isinstance(action.params["price_usd"], float)
