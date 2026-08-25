"""`social.caption_draft` (M8b slice 6, design §10 CMO / draft §3.3 #26) --
Claude writes a promo social caption for a PROVEN best-seller; the operator
copy-pastes it to IG/FB and posts MANUALLY. **No Meta/IG/FB call, no
publish, of any kind, anywhere** -- this capability's entire effect is one
`social.caption_drafted` event; `adapters/meta` is never imported. T2/
PROPOSE ceiling only, NEVER promotable, `undo` explicitly None (the reversal
is "don't post it"). Planner-only: propose() always []."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft
from shopsteward.pipeline.ops.governor import govern
from shopsteward.pipeline.ops.models import ProposedAction, RefusalReason, Tier
from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, StaleTargetError, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.ops.helpers import seed_sale_observed

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_SELLER = 901  # real sales this window -- a proven top seller
LISTING_NON_SELLER = 902  # never sold -- must never be proposed


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


def _seed_seller(
    conn,
    listing_id=LISTING_SELLER,
    units=5,
    title="Sandhill Cranes at Dawn Print",
    state="active",
    sale_day=None,
):
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    seed_listing_observed_on(
        conn, listing_id=listing_id, title=title, day=TODAY, views=100, state=state
    )
    seed_sale_observed(
        conn,
        receipt_id=90000 + listing_id,
        day=sale_day or TODAY,
        transactions=[(listing_id, 990000 + listing_id, units, 87.00)],
    )
    ops_config.seed(conn, USER_ID)  # execute() reads get_ops_config() -- must exist
    rebuild_core(conn)
    rebuild_ops(conn)


def _seed_rising_zero_sales_listing(conn, listing_id=903, title="Rising Listing, Never Sold"):
    """M5 (guardrail review, 2026-08-25): a listing proven ONLY by T12's
    views-velocity arm (proj_listings.proven_listings) -- zero sales, ever.
    delta=10 clears the default views_velocity_min_delta=5."""
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    for offset, views in ((-29, 10), (0, 20)):
        seed_listing_observed_on(
            conn,
            listing_id=listing_id,
            title=title,
            day=TODAY + timedelta(days=offset),
            views=views,
        )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def _intent(listing_id: str, channel: str = "instagram", **params) -> ProposalIntent:
    return ProposalIntent(
        capability_key="social.caption_draft",
        target_id=f"{listing_id}:{channel}",
        params=params,
        reason="the LLM's own sentence -- must never become the audit reason",
    )


# --- no Meta/publish/external call anywhere ----------------------------------


def test_module_never_imports_meta_or_any_external_adapter():
    import shopsteward.pipeline.ops.capabilities.caption_draft as mod

    with open(mod.__file__, encoding="utf-8") as f:
        src = f.read()
    for banned in ("adapters.meta", "adapters.etsy", "httpx", "requests"):
        assert banned not in src


def test_execute_only_appends_one_event_no_network(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    action = cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="Hi!"))
    assert action is not None

    before = read_all(conn)
    result = cap.execute(conn, USER_ID, action)
    after_events = read_all(conn, "social.caption_drafted")

    assert len(after_events) == 1
    assert after_events[0].payload["caption"] == "Hi!"
    assert after_events[0].payload["channel"] == "instagram"
    assert result.after == {
        "listing_id": LISTING_SELLER,
        "channel": "instagram",
        "chars": len("Hi!"),
    }
    assert result.cost_usd == 0.0
    # exactly one new event landed (the domain event) -- execute() itself
    # writes nothing else.
    assert len(read_all(conn)) == len(before) + 1


# --- eligibility: only proven top sellers ------------------------------------


def test_propose_always_returns_empty(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    assert cap.propose(conn, USER_ID, _cfg()) == []


def test_top_seller_is_a_valid_materialize_target(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="Fresh off the press!")
    )

    assert action is not None
    assert action.capability == "social.caption_draft"
    assert action.target_type == "listing"
    assert action.target_id == f"{LISTING_SELLER}:instagram"
    assert action.params == {
        "caption": "Fresh off the press!",
        "listing_id": LISTING_SELLER,
        "channel": "instagram",
    }
    assert "top seller (5 sold)" in action.reason
    assert "Fresh off the press!" not in action.reason  # the caption is never the audit reason
    assert action.estimated_cost_usd == 0.0
    assert action.undo_available is False


def test_a_sale_45_days_ago_the_old_7d_gate_excluded_is_now_a_valid_target(conn):
    """T12 (operator-approved 2026-08-25 /autoplan gate): a listing sold 45
    days ago -- outside the old revenue_window_days=7 gate, inside the new
    proven_window_days=90 -- must be materializable."""
    _seed_seller(conn, sale_day=TODAY - timedelta(days=45))
    cap = SocialCaptionDraft()

    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="Fresh off the press!")
    )

    assert action is not None
    assert "top seller (5 sold)" in action.reason


def test_views_velocity_zero_sales_listing_is_a_valid_target_with_honest_reason(conn):
    """M5 (guardrail review, 2026-08-25): the newly-widened views-velocity
    arm -- rising views, zero sales ever -- must still be a valid caption
    target, with a reason that never calls it a "top seller"."""
    _seed_rising_zero_sales_listing(conn)
    cap = SocialCaptionDraft()

    action = cap.materialize(conn, USER_ID, _cfg(), _intent("903", caption="Trending now!"))

    assert action is not None
    assert "rising views, no sales yet" in action.reason
    assert "top seller" not in action.reason


def test_non_seller_is_never_proposed(conn):
    _seed_seller(conn)  # only LISTING_SELLER gets real sales
    cap = SocialCaptionDraft()

    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_NON_SELLER), caption="Buy this!")
    )

    assert action is None


def test_an_inactive_top_seller_is_not_proposed(conn):
    # A listing that WAS a proven seller but has since been deactivated must
    # never get a promo caption -- that drives traffic to a delisted item.
    _seed_seller(conn, state="inactive")
    cap = SocialCaptionDraft()

    action = cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="Buy!"))

    assert action is None


def test_execute_refuses_a_top_seller_deactivated_between_propose_and_approve(conn):
    _seed_seller(conn)  # active at propose time
    cap = SocialCaptionDraft()
    action = cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="Buy!"))
    assert action is not None

    _seed_seller(conn, state="inactive")  # deactivated before approval

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, action)
    assert read_all(conn, "social.caption_drafted") == []


def test_a_hallucinated_listing_id_is_ungrounded(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    action = cap.materialize(conn, USER_ID, _cfg(), _intent("999999", caption="Buy this!"))

    assert action is None


# --- materialize drops: empty / non-str / oversized caption ------------------


def test_materialize_drops_empty_caption(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="")) is None


def test_materialize_drops_missing_caption(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER))) is None


def test_materialize_drops_whitespace_only_caption(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    assert (
        cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="   ")) is None
    )


def test_materialize_accepts_caption_with_real_content_and_surrounding_whitespace(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="  Buy now!  ")
    )

    assert action is not None
    assert action.params["caption"] == "  Buy now!  "  # original string preserved, not stripped


def test_materialize_drops_non_str_caption_even_bypassing_intent_validation(conn):
    """ProposalIntent's params typing already excludes most non-str/list/etc,
    but an int/float/bool DOES type-check into params -- materialize() must
    still reject it as a caption, never coerce it to str."""
    _seed_seller(conn)
    cap = SocialCaptionDraft()

    intent = _intent(str(LISTING_SELLER), caption=12345)
    assert cap.materialize(conn, USER_ID, _cfg(), intent) is None


def test_materialize_drops_oversized_caption_never_truncates(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg()
    too_long = "x" * (cfg.caption.max_len + 1)

    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption=too_long))

    assert action is None


def test_materialize_accepts_caption_exactly_at_the_limit(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg()
    exact = "x" * cfg.caption.max_len

    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption=exact))

    assert action is not None
    assert action.params["caption"] == exact


# --- registration: T2 only, undo=None ----------------------------------------


def test_registers_t2_with_no_undo():
    cap = SocialCaptionDraft()

    register(cap)

    assert REGISTRY["social.caption_draft"] is cap
    assert cap.max_tier == Tier.PROPOSE
    assert cap.undo is None
    assert cap.policy_verified is True


def test_estimate_cost_is_always_zero(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    action = cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="Hi"))
    assert action is not None

    assert cap.estimate_cost_usd(action) == 0.0


# --- e2e via the runner: lands at T2, approve appends the domain event ------


def test_e2e_lands_at_t2_then_approve_appends_caption_drafted_verbatim(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Just dropped -- link in bio!")
    )
    assert action is not None

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    assert report.proposed == 1
    assert report.executed == 0  # T2 -- never auto-executed

    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    assert approved.executed == 1

    drafted = [
        e
        for e in read_all(conn, "social.caption_drafted")
        if e.payload["listing_id"] == LISTING_SELLER
    ]
    assert len(drafted) == 1
    assert drafted[0].payload["caption"] == "Just dropped -- link in bio!"  # verbatim

    state = capability_states(conn, USER_ID)["social.caption_draft"]
    assert state.tier == Tier.PROPOSE  # never promoted


def test_never_promoted_above_t2_even_with_enough_approvals_and_elapsed_days(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    cfg.autonomy.ladder.promote_approvals = 1
    cfg.autonomy.ladder.promote_min_days = 1

    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Buy now!"))
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    later = TODAY + timedelta(days=2)
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=later)

    promoted = [e for e in read_all(conn, "capability.") if e.type == "capability.promoted"]
    assert promoted == []
    state = capability_states(conn, USER_ID)["social.caption_draft"]
    assert state.tier == Tier.PROPOSE


# --- execute() re-validation: a hand-forged/stale action never records junk -


def test_execute_refuses_a_hand_forged_action_with_invalid_caption(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg()
    forged = ProposedAction(
        action_id="forged-caption",
        capability="social.caption_draft",
        target_type="listing",
        target_id=f"{LISTING_SELLER}:instagram",
        tier=Tier.PROPOSE,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=False,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"caption": "x" * (cfg.caption.max_len + 1)},
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    assert read_all(conn, "social.caption_drafted") == []


def test_execute_refuses_when_the_listing_is_no_longer_a_top_seller(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    forged = ProposedAction(
        action_id="forged-caption-nonseller",
        capability="social.caption_draft",
        target_type="listing",
        target_id=f"{LISTING_NON_SELLER}:instagram",
        tier=Tier.PROPOSE,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=False,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"caption": "Buy this!"},
    )

    with pytest.raises(ValueError):
        cap.execute(conn, USER_ID, forged)

    assert read_all(conn, "social.caption_drafted") == []


# --- M3 (guardrail review, 2026-08-25): a legacy/malformed pending -------
# --- proposal must never burn on approve ------------------------------------


def test_a_legacy_bare_listing_id_pending_proposal_does_not_burn_on_approve(conn):
    """M3: the LIVE shop's NEEDS-YOU queue may hold a pre-T5 proposal whose
    target_id is a bare listing id (e.g. "12345", the format before channel
    identity existed) -- approve() must never terminalize it. Simulated by
    directly appending an `action.proposed` event with that legacy shape
    (never producible by today's materialize()/propose(), which always
    mints "{listing_id}:{channel}") -- append-only, real event."""
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    legacy_action = ProposedAction(
        action_id="legacy-bare-listing-id",
        capability="social.caption_draft",
        target_type="listing",
        target_id=str(LISTING_SELLER),  # legacy shape -- no ":channel"
        tier=Tier.PROPOSE,
        reason="pre-T5 legacy proposal",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=False,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"caption": "Buy this!"},
    )
    append(
        conn,
        Event(user_id=USER_ID, type="action.proposed", payload=legacy_action.model_dump()),
    )

    approved = approve_action(conn, USER_ID, "legacy-bare-listing-id", [cap], cfg=cfg, today=TODAY)

    assert approved.failed == 0
    statuses = {
        e.type
        for e in read_all(conn, "action.")
        if e.payload.get("action_id") == "legacy-bare-listing-id"
    }
    assert "action.failed" not in statuses
    assert "action.refused" in statuses


# --- H2 (guardrail review, 2026-08-25): cooldown/eligibility-mode is a -----
# --- governor REFUSAL (approvable again), never an execute()-time burn -----


def test_execute_raises_stale_target_error_for_a_genuinely_gone_listing(conn):
    """H2a: execute()'s own re-validation (`_stale_check()`) raises the
    dedicated `StaleTargetError` (not a plain ValueError) for genuine
    per-target staleness -- a deactivated listing."""
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    action = cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_SELLER), caption="Buy!"))
    assert action is not None
    _seed_seller(conn, state="inactive")

    with pytest.raises(StaleTargetError):
        cap.execute(conn, USER_ID, action)


def test_a_genuinely_stale_target_still_terminalizes_via_the_runner(conn):
    """H2 test requirement: run the FULL runner path (propose -> approve),
    not just a direct execute() call -- a listing deactivated after propose
    must still land a TERMINAL action.failed on approve, exactly as before
    this review's fix (only the ROUTE there -- StaleTargetError -- changed,
    never the outcome for genuine staleness)."""
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Buy!"))
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    _seed_seller(conn, state="inactive")  # genuinely stale between propose and approve

    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    assert approved.failed == 1
    statuses = {
        e.type for e in read_all(conn, "action.") if e.payload.get("action_id") == action_id
    }
    assert "action.failed" in statuses


def test_a_channel_eligibility_flip_is_a_refusal_that_stays_approvable(conn):
    """H2 test requirement: an approval that is ineligible only by CONFIG
    (a channel's eligibility mode flipped explore -> proven for a listing
    that isn't proven -- one of H2's own named reachable scenarios) must be
    REFUSED (governor.RefusalReason.INELIGIBLE), never terminalized -- and
    must still be approvable once the config condition changes back."""
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    LISTING_UNPROVEN = 908
    seed_listing_observed_on(
        conn, listing_id=LISTING_UNPROVEN, title="Never Sold, Flat Views", day=TODAY, views=10
    )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)

    cap = SocialCaptionDraft()
    cfg_explore = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    _channel_cfg(cfg_explore, "instagram", eligibility="explore")

    action = cap.materialize(
        conn, USER_ID, cfg_explore, _intent(str(LISTING_UNPROVEN), caption="Buy!")
    )
    assert action is not None
    run(conn, USER_ID, cfg_explore, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    # Now governed against a config where instagram is back to its "proven"
    # default -- LISTING_UNPROVEN never clears that bar.
    cfg_proven = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    decision = govern(conn, USER_ID, action, cap, cfg_proven, TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.INELIGIBLE

    approved1 = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg_proven, today=TODAY)
    assert approved1.refused == 1
    assert approved1.executed == 0

    # The crux of H2: never a terminal state for a policy-only refusal.
    statuses = {
        e.type for e in read_all(conn, "action.") if e.payload.get("action_id") == action_id
    }
    assert "action.failed" not in statuses
    assert "action.expired" not in statuses
    assert "action.rejected" not in statuses

    # And it is still approvable once the condition that refused it changes.
    approved2 = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg_explore, today=TODAY)
    assert approved2.executed == 1


def test_execute_time_non_stale_exception_is_a_refusal_not_a_failure(conn):
    """H2b structural guard: a capability's execute() raising anything OTHER
    than StaleTargetError (here, the existing invalid-caption ValueError)
    is recorded as a non-terminal action.refused by the runner, not a
    terminal action.failed -- pins the safe default so a future capability
    author's plain `raise ValueError(...)` mistake fails safe."""
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Buy!"))
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    # Corrupt the approved action's own params (hand-forged, bypassing
    # materialize()) so execute() raises the invalid-caption ValueError,
    # never StaleTargetError -- read straight from the event log
    # (core.events is append-only, so this simulates a forged execute()
    # call by invoking the capability directly for the assertion, then
    # exercises the REAL runner path for the actual outcome below).
    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    assert approved.executed == 1  # sanity: the happy path still executes

    # Directly exercise the runner's exception-classification behavior: a
    # capability whose execute() raises plain ValueError (not
    # StaleTargetError) must be recorded as action.refused, not
    # action.failed -- constructed against a second, still-open action_id.
    from shopsteward.pipeline.ops.models import ExecutionResult
    from shopsteward.pipeline.ops.runner import RunReport, _execute_and_record

    class _RaisesPlainValueError:
        key = "social.caption_draft"

        def execute(self, conn, user_id, action) -> ExecutionResult:
            raise ValueError("not a staleness problem")

    forged = ProposedAction(
        action_id="forged-non-stale-exception",
        capability="social.caption_draft",
        target_type="listing",
        target_id=f"{LISTING_SELLER}:instagram",
        tier=Tier.PROPOSE,
        reason="forged",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=False,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={},
    )
    report = RunReport()
    _execute_and_record(conn, USER_ID, _RaisesPlainValueError(), forged, report)

    assert report.refused == 1
    assert report.failed == 0
    refused_events = [
        e
        for e in read_all(conn, "action.refused")
        if e.payload.get("action_id") == "forged-non-stale-exception"
    ]
    assert len(refused_events) == 1
    assert refused_events[0].payload["reason"] == "execute_revalidation_error"
    assert not any(
        e.payload.get("action_id") == "forged-non-stale-exception"
        for e in read_all(conn, "action.failed")
    )


# --- undo on a no-undo capability must be graceful, never a crash -----------


def test_undo_on_a_caption_action_is_graceful_not_a_crash(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Buy now!"))
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    with pytest.raises(ValueError, match="no undo"):
        undo_action(conn, USER_ID, action_id, [cap])

    assert read_all(conn, "action.undone") == []


# --- the planner gate still drops unknown/ungrounded with this registered ---


def test_planner_gate_drops_unknown_and_ungrounded_with_caption_draft_registered(conn):
    _seed_seller(conn)
    register(SocialCaptionDraft())
    cfg = _cfg()
    cfg.autonomy.planner_max_per_capability_per_run = 5

    from shopsteward.adapters.planner.fake import FakePlannerAdapter
    from shopsteward.pipeline.ops.planner import plan_proposals

    intents = [
        ProposalIntent(
            capability_key="not.a.real.capability", target_id="whatever", params={}, reason="test"
        ),
        ProposalIntent(
            capability_key="social.caption_draft",
            target_id=str(LISTING_NON_SELLER),
            params={"caption": "Buy this!"},
            reason="test",
        ),
    ]
    adapter = FakePlannerAdapter(plan=intents)

    proposals = plan_proposals(
        conn, USER_ID, cfg, adapter, list(REGISTRY.values()), soft_cap_usd=1000.0
    )

    assert proposals == []
    reasons = {e.payload["reason"] for e in read_all(conn, "planner.intent_dropped")}
    assert "unknown_capability" in reasons
    # E10: split "ungrounded" -- LISTING_NON_SELLER was never a top_sellers
    # target, so this is a genuinely hallucinated target, not a stale one.
    assert "hallucinated_target" in reasons


# --- Brief: CAPTIONS TO POST section -----------------------------------------


def test_brief_shows_recent_caption_drafts(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Just dropped -- link in bio!")
    )
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    live_cfg = ops_config.get_ops_config(conn, USER_ID)
    brief = generate_brief(conn, USER_ID, live_cfg, as_of=TODAY)

    assert len(brief.caption_drafts) == 1
    assert brief.caption_drafts[0].caption == "Just dropped -- link in bio!"
    text = render_text(brief)
    assert "CAPTIONS TO POST (copy to IG/FB) (1)" in text
    assert "Just dropped -- link in bio!" in text


def test_brief_captions_section_disabled_by_config(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Buy!"))
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    live_cfg = ops_config.get_ops_config(conn, USER_ID)
    live_cfg = live_cfg.model_copy(
        update={"brief_sections": live_cfg.brief_sections.model_copy(update={"captions": False})}
    )
    brief = generate_brief(conn, USER_ID, live_cfg, as_of=TODAY)

    assert brief.caption_drafts == []
    text = render_text(brief)
    assert "CAPTIONS TO POST" not in text


def test_brief_seven_day_window_excludes_an_eight_day_old_caption(conn):
    import json

    _seed_seller(conn)
    old_day = TODAY - timedelta(days=8)
    payload = {
        "listing_id": LISTING_SELLER,
        "caption": "An old caption",
        "title": "Sandhill Cranes at Dawn Print",
        "drafted_at": f"{old_day.isoformat()}T00:00:00Z",
    }
    # Direct INSERT with an explicit historical created_at (helpers.py's
    # seed_listing_observed_on precedent) -- still append-only, never an
    # UPDATE/DELETE of an event row; core.events.append() always stamps
    # wall-clock "now", which can't place a row 8 days in the past.
    old_created_at = f"{old_day.isoformat()}T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (USER_ID, "social.caption_drafted", json.dumps(payload), old_created_at),
    )
    conn.commit()

    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    live_cfg = ops_config.get_ops_config(conn, USER_ID)
    brief = generate_brief(conn, USER_ID, live_cfg, as_of=TODAY)

    assert brief.caption_drafts == []


def test_existing_brief_fields_stay_green_new_field_defaults_empty(conn):
    """Regression: a Brief built without ever touching captions leaves the
    new field at its default empty list."""
    from shopsteward.pipeline.ops.models import Brief

    brief = Brief(generated_at=TODAY, window_days=7)
    assert brief.caption_drafts == []


# --- no secret in any payload, user_id on every event, append-only ----------


def test_no_secret_in_any_payload_and_user_id_on_every_event(conn):
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Just dropped -- link in bio!")
    )
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    import json

    banned = ("token", "api_key", "apikey", "secret", "signed_url", "access_token", "refresh_token")
    for e in read_all(conn):
        blob = json.dumps(e.payload).lower()
        for word in banned:
            assert word not in blob, f"event {e.type} payload leaked {word!r}: {blob}"
        assert e.user_id == USER_ID


# --- T5+E5 (2026-08-25): channel identity, mark_posted, per-channel cooldown -


def _channel_cfg(cfg, channel: str, **overrides):
    """Mutates cfg.caption.channels[channel] in place (OpsConfig is a plain,
    non-frozen pydantic model) and returns cfg, for a terser test body."""
    updated = cfg.caption.channels[channel].model_copy(update=overrides)
    cfg.caption.channels[channel] = updated
    return cfg


def test_two_channels_same_listing_are_both_proposable_and_never_deduped(conn):
    """T5 Eng Decision #9: channel is part of the target identity
    ("{listing_id}:{channel}") so an instagram draft and a facebook draft
    for the SAME listing never collide in the runner's (capability,
    target_id) dedup -- both must land as independent, separately
    approvable T2 proposals."""
    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action_ig = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_SELLER), channel="instagram", caption="IG copy")
    )
    action_fb = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_SELLER), channel="facebook", caption="FB copy")
    )
    assert action_ig is not None
    assert action_fb is not None
    assert action_ig.target_id == f"{LISTING_SELLER}:instagram"
    assert action_fb.target_id == f"{LISTING_SELLER}:facebook"
    assert action_ig.action_id != action_fb.action_id

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action_ig, action_fb])

    assert report.proposed == 2
    assert report.skipped_duplicate_target == 0
    proposed_target_ids = {e.payload["target_id"] for e in read_all(conn, "action.proposed")}
    assert proposed_target_ids == {f"{LISTING_SELLER}:instagram", f"{LISTING_SELLER}:facebook"}


def test_proven_policy_default_excludes_a_never_sold_never_risen_listing(conn):
    """Both shipped channels default to eligibility="proven" -- an active
    listing with no sales and no views-velocity signal is never a candidate
    on either channel (module docstring's owned-network argument)."""
    from shopsteward.pipeline.ops.capabilities.caption_draft import _candidates
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    seed_listing_observed_on(
        conn, listing_id=905, title="Never Sold, Flat Views", day=TODAY, views=10
    )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg()

    targets = _candidates(conn, USER_ID, cfg)

    assert "905:instagram" not in targets
    assert "905:facebook" not in targets


def test_explore_policy_proposes_an_unproven_active_listing(conn):
    """A channel configured eligibility="explore" is coverage-first, like
    social.pinterest_post -- an active listing with zero sales/views-signal
    IS a candidate on that channel (never on a sibling channel left at the
    "proven" default)."""
    from shopsteward.pipeline.ops.capabilities.caption_draft import _candidates
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    LISTING_UNPROVEN = 906
    seed_listing_observed_on(
        conn, listing_id=LISTING_UNPROVEN, title="Never Sold, Flat Views", day=TODAY, views=10
    )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg()
    _channel_cfg(cfg, "instagram", eligibility="explore")

    targets = _candidates(conn, USER_ID, cfg)

    assert f"{LISTING_UNPROVEN}:instagram" in targets
    assert f"{LISTING_UNPROVEN}:facebook" not in targets  # facebook stays "proven"


def test_a_drafted_but_never_posted_caption_blocks_recandidacy_for_the_cooldown(conn):
    """Requirement 3/policy choice: cooldown is keyed off DRAFTED-OR-POSTED,
    not posted-only -- a proposal the operator never even approved still
    holds the (listing, channel) cooldown, mirroring social.pinterest_post's
    own "recently offered" anti-spam property. Exercised via a real
    events.append() write (not a hand-forged row) with an injected clock,
    per the task's own test requirement."""
    from shopsteward.pipeline.ops.capabilities.caption_draft import _candidates

    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg()
    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Buy now!"))
    assert action is not None
    cap.execute(conn, USER_ID, action)  # drafted -- never approved, never mark_posted()'d

    just_after_draft = datetime.now(UTC)
    targets = _candidates(conn, USER_ID, cfg, now=just_after_draft)

    assert f"{LISTING_SELLER}:instagram" not in targets


def test_cooldown_boundary_exactly_at_the_edge_still_blocks_one_second_past_releases(conn):
    """Real events.append() write (so the DB's real created_at format is
    exercised, per the task's own test requirement), injected clock at the
    exact cooldown_days cutoff (still blocked, `>=`) and one second past it
    (released) -- mirrors pinterest_post's own boundary-exactness test
    philosophy (E2)."""
    from shopsteward.pipeline.ops.capabilities.caption_draft import _candidates
    from shopsteward.pipeline.ops.timeutil import parse_ts

    _seed_seller(conn)
    cap = SocialCaptionDraft()
    cfg = _cfg()
    cooldown_days = cfg.caption.channels["instagram"].cooldown_days
    action = cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_SELLER), caption="Buy now!"))
    assert action is not None
    cap.execute(conn, USER_ID, action)
    # Read the REAL stored created_at (core/db.py's own default-stamped
    # value) rather than a wall-clock guess taken after the call returns --
    # the exact-edge assertion below needs the SAME instant append() used.
    (drafted_event,) = read_all(conn, "social.caption_drafted")
    drafted_at = parse_ts(drafted_event.created_at)

    at_the_edge = drafted_at + timedelta(days=cooldown_days)
    still_within = _candidates(conn, USER_ID, cfg, now=at_the_edge)
    assert f"{LISTING_SELLER}:instagram" not in still_within

    just_past_the_edge = at_the_edge + timedelta(seconds=1)
    released = _candidates(conn, USER_ID, cfg, now=just_past_the_edge)
    assert f"{LISTING_SELLER}:instagram" in released


# --- mark_posted() (T5+E5) ---------------------------------------------------


def _draft_a_real_caption(conn, listing_id=LISTING_SELLER, channel="instagram") -> str:
    """Runs the real materialize -> propose -> approve path (test_mark_posted.py's
    _draft_a_real_pin precedent) so the returned action_id is a genuine,
    resolvable one."""
    _seed_seller(conn, listing_id=listing_id)
    cap = SocialCaptionDraft()
    register(cap)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)
    action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(listing_id), channel=channel, caption="Buy now!")
    )
    assert action is not None
    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)
    return action_id


def test_mark_posted_is_idempotent(conn):
    from shopsteward.pipeline.ops.capabilities.caption_draft import mark_posted

    action_id = _draft_a_real_caption(conn)

    assert mark_posted(conn, USER_ID, action_id) is True
    assert mark_posted(conn, USER_ID, action_id) is False

    posted = [
        e for e in read_all(conn, "social.caption_posted") if e.payload["action_id"] == action_id
    ]
    assert len(posted) == 1  # never double-appended
    assert posted[0].payload["listing_id"] == LISTING_SELLER


def test_mark_posted_unknown_action_id_raises_clearly(conn):
    from shopsteward.pipeline.ops.capabilities.caption_draft import mark_posted

    with pytest.raises(ValueError, match="no drafted caption found"):
        mark_posted(conn, USER_ID, "not-a-real-action-id")
    assert read_all(conn, "social.caption_posted") == []  # no partial state


# --- E4 (2026-08-25): --posted-at works for the caption channel too --------


def test_mark_posted_with_an_explicit_posted_at_stores_it(conn):
    from shopsteward.pipeline.ops.capabilities.caption_draft import mark_posted

    action_id = _draft_a_real_caption(conn)
    drafted_at = next(
        e.payload["drafted_at"]
        for e in read_all(conn, "social.caption_drafted")
        if e.payload["action_id"] == action_id
    )

    assert mark_posted(conn, USER_ID, action_id, posted_at=drafted_at) is True

    posted = [
        e for e in read_all(conn, "social.caption_posted") if e.payload["action_id"] == action_id
    ]
    assert posted[0].payload["posted_at"] == drafted_at


def test_mark_posted_rejects_a_future_posted_at_without_writing(conn):
    from shopsteward.pipeline.ops.capabilities.caption_draft import mark_posted

    action_id = _draft_a_real_caption(conn)
    future = (TODAY + timedelta(days=30)).isoformat()

    with pytest.raises(ValueError, match="future"):
        mark_posted(conn, USER_ID, action_id, posted_at=future)
    assert read_all(conn, "social.caption_posted") == []


# --- brief: CAPTIONS TO POST reflects channel + mark-posted queue -----------


# --- H1 (guardrail review, 2026-08-25): a pre-T5 config must never leave --
# --- caption_draft silently, permanently dead -----------------------------


def test_a_pre_t5_config_missing_channels_is_repaired_by_apply_not_silently_empty(conn):
    """A config seeded before `caption.channels` existed (write-once seed())
    would, with the old `default_factory=dict`, validate straight into an
    empty channel map -- `_candidates()` then iterates nothing, forever,
    with no error and no signal (H1). `channels` is now a REQUIRED field, so
    a stored config missing it fails `OpsConfig.model_validate()` and
    `config.apply()`'s EXISTING schema-drift auto-repair (test_config.py's
    own `test_apply_treats_a_stored_config_that_no_longer_validates_as_
    changed` precedent) kicks in on the next `ops config apply` -- the
    documented upgrade path, never a silent, permanent dead capability."""
    from shopsteward.core.events import Event, append

    _seed_seller(conn)  # a real proven seller, seeded before the repair

    old_shaped = ops_config.load_ops_config().model_dump(by_alias=True)
    del old_shaped["caption"]["channels"]
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="opsconfig.seeded",
            payload={"name": old_shaped["name"], "config": old_shaped, "source": "defaults"},
        ),
    )

    # Sanity: the pre-repair stored config genuinely no longer validates --
    # this is the exact condition config.apply() must detect and repair.
    from pydantic import ValidationError

    from shopsteward.pipeline.ops.models import OpsConfig

    with pytest.raises(ValidationError):
        OpsConfig.model_validate(old_shaped)

    assert ops_config.apply(conn, USER_ID) is True  # the documented upgrade path
    rebuild_ops(conn)

    repaired_cfg = ops_config.get_ops_config(conn, USER_ID)
    assert repaired_cfg.caption.channels  # never silently empty again

    cap = SocialCaptionDraft()
    targets = cap.materialize(
        conn, USER_ID, repaired_cfg, _intent(str(LISTING_SELLER), caption="Buy now!")
    )
    assert targets is not None  # the capability is no longer silently inert


def test_brief_captions_show_channel_and_drop_once_marked_posted(conn):
    from shopsteward.pipeline.ops.capabilities.caption_draft import mark_posted

    action_id = _draft_a_real_caption(conn, channel="facebook")

    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cfg_live = ops_config.get_ops_config(conn, USER_ID)
    brief_before = generate_brief(conn, USER_ID, cfg_live, as_of=TODAY)
    assert [c.action_id for c in brief_before.caption_drafts] == [action_id]
    assert brief_before.caption_drafts[0].channel == "facebook"
    text_before = render_text(brief_before)
    assert "[facebook]" in text_before
    assert f"ops mark-posted {action_id}" in text_before

    mark_posted(conn, USER_ID, action_id)

    brief_after = generate_brief(conn, USER_ID, cfg_live, as_of=TODAY)
    assert brief_after.caption_drafts == []
