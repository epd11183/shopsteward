"""M8b slice 2 -- the planner validation gate (design §2/§6, the
safety-critical test of this milestone). An LLM proposal for an unknown/
prohibited/hallucinated action must be dropped BEFORE it can become an
`action.proposed`, and every drop is logged as `planner.intent_dropped`.
Entirely on fakes -- zero network."""

import json
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
from tests.pipeline.ops.helpers import seed_listing_observed_on, seed_sale_observed
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
    # E10: split "ungrounded" -- target_id 999999 was never in
    # listing.autorenew_off's own propose() targets (only 601 is), so this
    # is a genuinely hallucinated target, not merely a stale candidate.
    assert ("hallucinated_target", "listing.autorenew_off") in reasons
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
    rebuild_core(conn)
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
    rebuild_core(conn)
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
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg(enabled=True)
    cap = StubCapability()
    register(cap)
    adapter = FakePlannerAdapter(plan=PlannerParseError("boom"))

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert proposals == []
    assert read_all(conn, "llm.call") == []


def test_facts_json_includes_viewed_not_sold_for_seo_edit_target_discovery(conn):
    """listing.seo_edit's propose() always returns [] (planner-only) -- the
    only way the LLM can ever name a target for it is a real-data block in
    facts_json. Same signal listing.reprice's own eligibility keys on."""
    LISTING_VIEWED_NOT_SOLD = 701
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_VIEWED_NOT_SOLD,
        title="Loon at Dusk Digital Download",
        day=TODAY - timedelta(days=1),
        views=42,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg(enabled=True)
    adapter = FakePlannerAdapter(plan=[])

    plan_proposals(conn, USER_ID, cfg, adapter, [], soft_cap_usd=10.0)

    (facts_json,) = adapter.plan_calls
    facts = json.loads(facts_json)
    assert facts["viewed_not_sold"] == [
        {
            "listing_id": LISTING_VIEWED_NOT_SOLD,
            "title": "Loon at Dusk Digital Download",
            "views_lifetime": 42,
        }
    ]
    # deterministic Brief/other blocks unaffected -- still present, unchanged shape.
    assert "dead_listings" in facts
    assert "trending" in facts


def test_facts_json_includes_expired_with_sales_for_seo_edit_target_discovery(conn):
    """listing.seo_edit's other eligibility branch (expired + real historical
    sales) -- same real target ids listing.renew proposes for reactivation."""
    LISTING_EXPIRED_WITH_SALES = 702
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_EXPIRED_WITH_SALES,
        title="Old Canvas Print",
        day=TODAY - timedelta(days=1),
        views=0,
        state="expired",
        quantity=5,
    )
    seed_sale_observed(
        conn,
        receipt_id=9101,
        day=TODAY - timedelta(days=100),
        transactions=[(LISTING_EXPIRED_WITH_SALES, 91011, 1, 40.0)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg(enabled=True)
    adapter = FakePlannerAdapter(plan=[])

    plan_proposals(conn, USER_ID, cfg, adapter, [], soft_cap_usd=10.0)

    (facts_json,) = adapter.plan_calls
    facts = json.loads(facts_json)
    assert facts["expired_with_sales"] == [
        {
            "listing_id": LISTING_EXPIRED_WITH_SALES,
            "title": "Old Canvas Print",
            "lifetime_sales": 1,
        }
    ]


def test_proven_listings_facts_block_and_grounded_ids_use_the_widened_proven_set(conn):
    """T12 (operator-approved 2026-08-25 /autoplan gate): a listing sold 45
    days ago -- outside the old revenue_window_days=7 gate, inside the new
    proven_window_days=90 -- must appear in facts_json's "proven_listings"
    block (M3: renamed from "top_sellers" -- that label sent verbatim to
    the copy LLM is a plausible route to "bestseller" copy for a listing
    that has never sold) AND be accepted (not dropped `not_a_candidate`/
    `hallucinated_target`) as a social.caption_draft intent target, since
    materialize() now grounds against the same widened set."""
    from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft

    LISTING_WIDENED = 703
    seed_listing_observed_on(
        conn, listing_id=LISTING_WIDENED, title="Widened Seller", day=TODAY, views=10
    )
    seed_sale_observed(
        conn,
        receipt_id=9102,
        day=TODAY - timedelta(days=45),
        transactions=[(LISTING_WIDENED, 91021, 1, 87.0)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    register(SocialCaptionDraft())
    cfg = _cfg(enabled=True)
    # T5+E5 (2026-08-25): materialize() now accepts a composite
    # "{listing_id}:{channel}" target_id, never a bare listing_id -- see
    # caption_draft.py's own target-identity docstring.
    target_id = f"{LISTING_WIDENED}:instagram"
    adapter = FakePlannerAdapter(
        plan=[
            ProposalIntent(
                capability_key="social.caption_draft",
                target_id=target_id,
                params={"caption": "Fresh off the press!"},
                reason="widened proven seller",
            )
        ]
    )

    proposals = plan_proposals(
        conn, USER_ID, cfg, adapter, list(REGISTRY.values()), soft_cap_usd=10.0
    )

    (facts_json,) = adapter.plan_calls
    facts = json.loads(facts_json)
    assert LISTING_WIDENED in {row["listing_id"] for row in facts["proven_listings"]}
    assert target_id in {row["target_id"] for row in facts["caption_eligible_targets"]}
    assert [p.target_id for p in proposals] == [target_id]
    assert _dropped(conn) == []


def test_proven_listings_facts_block_labels_a_zero_sales_row_honestly(conn):
    """M3: a listing proven ONLY by the views-velocity arm (rising views,
    zero sales ever) must show up in facts_json's "proven_listings" block
    with units=0/revenue_usd=0.0 -- never silently rounded up, and never
    under the old "top_sellers" key, which would assert something false
    about a listing that has never sold."""
    LISTING_RISING = 704
    for offset, views in ((-29, 10), (0, 20)):  # delta=10 >= default min_delta=5
        seed_listing_observed_on(
            conn,
            listing_id=LISTING_RISING,
            title="Rising, Never Sold",
            day=TODAY + timedelta(days=offset),
            views=views,
        )
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg(enabled=True)
    adapter = FakePlannerAdapter(plan=[])

    plan_proposals(conn, USER_ID, cfg, adapter, list(REGISTRY.values()), soft_cap_usd=10.0)

    (facts_json,) = adapter.plan_calls
    facts = json.loads(facts_json)
    assert "top_sellers" not in facts
    (row,) = [r for r in facts["proven_listings"] if r["listing_id"] == LISTING_RISING]
    assert row["units"] == 0
    assert row["revenue_usd"] == 0.0


# --- E10: split "ungrounded" into hallucinated vs not-a-candidate -----------


def test_never_offered_target_is_dropped_as_hallucinated(conn):
    """A target_id never in ANY facts-json block this capability draws from
    -- genuinely invented by the LLM, not merely stale."""
    _seed_dead(conn, LISTING_DEAD_A)
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingAutorenewOff(FakeEtsyWriteAdapter())
    register(cap)
    cfg = _cfg(enabled=True)
    adapter = FakePlannerAdapter(
        plan=[
            ProposalIntent(
                capability_key="listing.autorenew_off", target_id="424242", reason="invented"
            )
        ]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert proposals == []
    dropped = _dropped(conn)
    assert len(dropped) == 1
    assert dropped[0].payload["reason"] == "hallucinated_target"


def test_offered_but_no_longer_eligible_target_is_dropped_as_not_a_candidate(conn):
    """A target_id that WAS in the pin-eligible facts block (still is, at
    materialize() time too) but whose params are invalid/unknown -- not a
    hallucinated id, just not a valid proposal for it right now."""
    from shopsteward.pipeline.ops.capabilities.pinterest_post import SocialPinterestPost

    listing_id = 801
    seed_listing_observed_on(conn, listing_id=listing_id, title="Loon Print", day=TODAY, views=10)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.images.observed",
            payload={
                "listing_id": listing_id,
                "images": [
                    {"listing_image_id": 1, "rank": 1, "url_570xN": "https://example.com/i.jpg"}
                ],
            },
        ),
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = SocialPinterestPost()
    register(cap)
    cfg = _cfg(enabled=True)
    adapter = FakePlannerAdapter(
        plan=[
            ProposalIntent(
                capability_key="social.pinterest_post",
                target_id=str(listing_id),
                params={
                    "title": "x",
                    "description": "x",
                    "alt_text": "x",
                    "board_key": "not_a_real_board",
                },
                reason="valid target, bad board",
            )
        ]
    )

    proposals = plan_proposals(conn, USER_ID, cfg, adapter, [cap], soft_cap_usd=10.0)

    assert proposals == []
    dropped = _dropped(conn)
    assert len(dropped) == 1
    assert dropped[0].payload["reason"] == "not_a_candidate"


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
    # autorenew_off + deactivate both target the dead/active listing (reprice
    # excludes it as non-digital-titled; seo_edit proposes nothing
    # deterministically; tune_threshold has nothing to trigger it).
    assert len(read_all(conn, "action.proposed")) == 2


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
    # autorenew_off + deactivate both target the dead/active listing -- see
    # the sibling test's comment above.
    assert len(read_all(conn, "action.proposed")) == 2
