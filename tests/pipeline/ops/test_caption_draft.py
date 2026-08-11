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
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft
from shopsteward.pipeline.ops.models import ProposedAction, Tier
from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
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
):
    from tests.pipeline.ops.helpers import seed_listing_observed_on

    seed_listing_observed_on(
        conn, listing_id=listing_id, title=title, day=TODAY, views=100, state=state
    )
    seed_sale_observed(
        conn,
        receipt_id=90000 + listing_id,
        day=TODAY,
        transactions=[(listing_id, 990000 + listing_id, units, 87.00)],
    )
    ops_config.seed(conn, USER_ID)  # execute() reads get_ops_config() -- must exist
    rebuild_core(conn)
    rebuild_ops(conn)


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def _intent(target_id: str, **params) -> ProposalIntent:
    return ProposalIntent(
        capability_key="social.caption_draft",
        target_id=target_id,
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
    assert result.after == {"listing_id": LISTING_SELLER, "chars": len("Hi!")}
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
    assert action.target_id == str(LISTING_SELLER)
    assert action.params == {"caption": "Fresh off the press!", "listing_id": LISTING_SELLER}
    assert "top seller (5 sold)" in action.reason
    assert "Fresh off the press!" not in action.reason  # the caption is never the audit reason
    assert action.estimated_cost_usd == 0.0
    assert action.undo_available is False


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
        target_id=str(LISTING_SELLER),
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
        target_id=str(LISTING_NON_SELLER),
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
    assert "ungrounded" in reasons


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
