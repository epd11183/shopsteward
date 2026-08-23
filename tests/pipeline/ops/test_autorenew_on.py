"""`listing.autorenew_on` -- turns auto-renew back ON for an active listing
that currently has it off AND whose MOST RECENT renewal-posture
`action.executed` event (among `listing.renew`, `listing.autorenew_off`,
`listing.autorenew_on`) for that specific listing_id was `listing.renew`
(e.g. listings brought back via `listing.renew`, whose `should_auto_renew`
was off from the start -- that's why they expired). This ORDINAL gate
exists to prevent an oscillation loop with `listing.autorenew_off` (see
autorenew_on.py's module docstring) -- a dead listing that `autorenew_off`
just turned off must NEVER be re-proposed here just because it's now
`state=='active' AND should_auto_renew==False`: `autorenew_off` executing
makes ITS action the most recent renewal-posture action for that
listing_id, which is never `listing.renew`, so that listing can never pass
this gate again -- even if it was genuinely renewed at some earlier point.
Only a fresh `listing.renew` execution AFTER that `autorenew_off` re-arms
it. Entirely on FakeEtsyWriteAdapter, zero network."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListingUpdate
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
from shopsteward.pipeline.ops.capabilities.autorenew_on import ListingAutorenewOn
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_ACTIVE_OFF = 701  # active, auto-renew OFF, HAS a real renew event -- SHOULD be proposed
LISTING_ACTIVE_ON = 702  # active, auto-renew already ON -- should NOT
LISTING_EXPIRED_OFF = 703  # expired, auto-renew OFF -- should NOT (not active)
LISTING_DEAD_NO_SALES = 704  # active, auto-renew OFF, dead, NO renew event -- should NOT


def _seed_renewal_posture_executed(
    conn, *, capability: str, listing_id: int, action_id: str, user_id: int = USER_ID
) -> None:
    """Seeds an `action.proposed` + `action.executed` pair for one of the
    three renewal-posture capabilities (`listing.renew`,
    `listing.autorenew_off`, `listing.autorenew_on`) for `listing_id`.
    `action_id` must be unique per call (event insertion order is what makes
    a later call here "more recent" -- the ordinal gate under test) --
    callers pass a distinct id per seeded event, not a listing-derived one,
    so a single listing can have several of these across a test.

    `cost_usd` is seeded as 0.0 regardless of capability: a renewal's real
    cost is already exercised in `listing.renew`'s own test suite, and
    charging it again here would spuriously eat into
    `governor.month_spend()`'s `monthly_spend_cap_usd` for tests that share
    the committed default config (see
    test_e2e_run_approve_executes_via_the_full_pipeline)."""
    append(
        conn,
        Event(
            user_id=user_id,
            type="action.proposed",
            payload={
                "action_id": action_id,
                "capability": capability,
                "target_type": "listing",
                "target_id": str(listing_id),
                "tier": 1,  # Tier.NOTIFY
                "reason": capability,
                "inputs_hash": "irrelevant",
                "estimated_cost_usd": 0.0,
                "undo_available": True,
                "expires_at": (TODAY + timedelta(days=30)).isoformat(),
                "params": {},
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=user_id,
            type="action.executed",
            payload={
                "action_id": action_id,
                "before": {},
                "after": {},
                "cost_usd": 0.0,
                "duration_ms": 0,
            },
        ),
    )


def _seed_renew_executed(conn, *, listing_id: int, user_id: int = USER_ID) -> None:
    """Seeds a real `listing.renew` action.proposed + action.executed pair
    for `listing_id` -- the only way `_candidates()` can now find this
    listing eligible (module docstring), UNLESS a later renewal-posture
    action supersedes it (the ordinal gate)."""
    _seed_renewal_posture_executed(
        conn,
        capability="listing.renew",
        listing_id=listing_id,
        action_id=f"renew-{listing_id}",
        user_id=user_id,
    )


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
        listing_id=LISTING_ACTIVE_OFF,
        title="Renewed Print",
        day=TODAY - timedelta(days=1),
        views=10,
        state="active",
        should_auto_renew=False,
    )
    _seed_renew_executed(conn, listing_id=LISTING_ACTIVE_OFF)
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_ACTIVE_ON,
        title="Already On Print",
        day=TODAY - timedelta(days=1),
        views=10,
        state="active",
        should_auto_renew=True,
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_EXPIRED_OFF,
        title="Still Expired Print",
        day=TODAY - timedelta(days=1),
        views=10,
        state="expired",
        should_auto_renew=False,
    )
    # LISTING_DEAD_NO_SALES: flat views over 100+ days, no sales ever, active,
    # auto-renew off, and crucially NO listing.renew execution event --
    # exactly what analytics.dead_listings() flags AND exactly the
    # naive-inverse eligibility that used to cause the oscillation loop with
    # autorenew_off (module docstring).
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_DEAD_NO_SALES,
        title="Forgotten Print",
        day=TODAY - timedelta(days=100),
        views=5,
        state="active",
        should_auto_renew=False,
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_DEAD_NO_SALES,
        title="Forgotten Print",
        day=TODAY - timedelta(days=1),
        views=5,
        state="active",
        should_auto_renew=False,
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


def test_propose_targets_only_active_autorenew_off_listings(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    cap = ListingAutorenewOn(fake)

    actions = cap.propose(conn, USER_ID, _cfg())

    target_ids = {a.target_id for a in actions}
    assert target_ids == {str(LISTING_ACTIVE_OFF)}
    action = actions[0]
    assert action.capability == "listing.autorenew_on"
    assert action.target_type == "listing"
    assert action.undo_available is True
    assert action.reason


# --- 1b. oscillation-loop prevention (the reviewer's finding) ------------------


def test_dead_listing_is_never_proposed_even_though_it_matches_the_naive_eligibility(conn):
    """A listing that satisfies analytics.dead_listings() (0 sales in-window,
    flat views) AND the naive `state=='active' AND should_auto_renew==False`
    eligibility -- exactly the state autorenew_off just put it in -- must
    NEVER be proposed by autorenew_on. Failing this test reproduces the
    reviewer's oscillation bug."""
    _seed_scenario(conn)
    cfg = _cfg()

    dead = analytics.dead_listings(conn, USER_ID, cfg, as_of=TODAY)
    assert LISTING_DEAD_NO_SALES in {d.listing_id for d in dead}

    cap = ListingAutorenewOn(FakeEtsyWriteAdapter())
    actions = cap.propose(conn, USER_ID, cfg)

    target_ids = {a.target_id for a in actions}
    assert str(LISTING_DEAD_NO_SALES) not in target_ids


LISTING_FLIP = 705  # a real dead_listings() candidate, used for the flag-flip test below
LISTING_GENUINELY_RENEWED = 706  # has a real listing.renew execution event


def test_off_and_on_can_never_both_target_the_same_listing_across_a_flag_flip(conn):
    """The reviewer's exact repro shape, anchored on REAL wall-clock "today"
    (never the stale tests/pipeline/ops/helpers.py AS_OF fixture constant --
    `analytics.dead_listings()` and this capability's own `_candidates()`
    both call `datetime.now(UTC)` internally with no `as_of` override, so an
    old fixture date would make this probe pass vacuously).

    Phase 1: a real dead_listings() candidate (0 sales/flat views for the
    full 180d window, >90d observed) with should_auto_renew=True.
    autorenew_off must propose it; autorenew_on must not (it was never
    renewed via listing.renew).

    Phase 2: the SAME listing_id, now observed with should_auto_renew=False
    (simulating the OFF action having executed) but with NO listing.renew
    execution event seeded. This is the actual fix under test: absence of a
    renew event, not a sales count, is what keeps autorenew_on from
    re-proposing it -- the oscillation loop the reviewer found is broken
    here."""
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_FLIP,
        title="Flip Print",
        day=TODAY - timedelta(days=100),
        views=5,
        state="active",
        should_auto_renew=True,
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_FLIP,
        title="Flip Print",
        day=TODAY - timedelta(days=1),
        views=5,
        state="active",
        should_auto_renew=True,
    )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg()

    dead = analytics.dead_listings(conn, USER_ID, cfg)
    assert LISTING_FLIP in {d.listing_id for d in dead}

    off_cap = ListingAutorenewOff(FakeEtsyWriteAdapter())
    on_cap = ListingAutorenewOn(FakeEtsyWriteAdapter())

    off_targets = {a.target_id for a in off_cap.propose(conn, USER_ID, cfg)}
    on_targets = {a.target_id for a in on_cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_FLIP) in off_targets
    assert str(LISTING_FLIP) not in on_targets

    # Phase 2: the OFF action's effect, no listing.renew event seeded.
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_FLIP,
        title="Flip Print",
        day=TODAY,
        views=5,
        state="active",
        should_auto_renew=False,
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    on_targets_after = {a.target_id for a in on_cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_FLIP) not in on_targets_after  # the fix: still excluded

    off_targets_after = {a.target_id for a in off_cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_FLIP) not in off_targets_after  # already off -- nothing left to stop


LISTING_RENEWED_THEN_DEAD_AGAIN = 707  # renewed once, went dead again ON -- the attempt-3 repro


def test_renewed_listing_that_goes_dead_again_is_not_reproposed_after_off_but_rearms_on_fresh_renew(
    conn,
):
    """The reviewer's exact attempt-3 repro (see autorenew_on.py's module
    docstring): a listing with a REAL, real `listing.renew` execution in its
    past, which then auto-renewed normally for a while (should_auto_renew
    stayed True the whole time -- a fully legitimate state, not a re-flip),
    and THEN independently went dead again while still active/on.

    `autorenew_off` must still be free to turn it off (it has no exclusion
    for previously-renewed listings). Once it does, `autorenew_on` must NOT
    re-target it -- the existential "was it ever renewed" check (attempt 3)
    would wrongly say yes here and oscillate; the ordinal "what's the MOST
    RECENT renewal-posture action" check must say `autorenew_off`, most
    recent, and refuse.

    Only a genuinely FRESH `listing.renew` execution, strictly after that
    `autorenew_off`, may re-arm eligibility -- confirmed in the final phase
    below, proving the ordinal gate swings both ways rather than being a
    permanent block. The final phase also seeds a fresh, growing-views
    observation alongside the fresh renew (F2, operator decision): a real
    renewal is followed by a real resync, and this listing must show it is
    no longer currently dead by the separate windowed check, or F2's
    exclusion would correctly keep it out regardless of the ordinal gate."""
    # A real renew execution, in the past.
    _seed_renew_executed(conn, listing_id=LISTING_RENEWED_THEN_DEAD_AGAIN)

    # Auto-renewed normally afterward, then independently went dead again
    # while active/on -- a real dead_listings() candidate.
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_RENEWED_THEN_DEAD_AGAIN,
        title="Renewed Then Dead Again Print",
        day=TODAY - timedelta(days=100),
        views=5,
        state="active",
        should_auto_renew=True,
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_RENEWED_THEN_DEAD_AGAIN,
        title="Renewed Then Dead Again Print",
        day=TODAY - timedelta(days=1),
        views=5,
        state="active",
        should_auto_renew=True,
    )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg()

    dead = analytics.dead_listings(conn, USER_ID, cfg)
    assert LISTING_RENEWED_THEN_DEAD_AGAIN in {d.listing_id for d in dead}

    off_cap = ListingAutorenewOff(FakeEtsyWriteAdapter())
    on_cap = ListingAutorenewOn(FakeEtsyWriteAdapter())

    # autorenew_off correctly targets it -- no exclusion for previously-renewed listings.
    off_targets = {a.target_id for a in off_cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_RENEWED_THEN_DEAD_AGAIN) in off_targets

    # The off action executes (seeded directly -- equivalent to execute()).
    _seed_renewal_posture_executed(
        conn,
        capability="listing.autorenew_off",
        listing_id=LISTING_RENEWED_THEN_DEAD_AGAIN,
        action_id=f"off-{LISTING_RENEWED_THEN_DEAD_AGAIN}-1",
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_RENEWED_THEN_DEAD_AGAIN,
        title="Renewed Then Dead Again Print",
        day=TODAY,
        views=5,
        state="active",
        should_auto_renew=False,
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    # The fix under test: autorenew_on must NOT re-target it -- attempt 3's
    # existential check would wrongly say yes (it has a real renew execution
    # somewhere in its history); the ordinal check says the most recent
    # renewal-posture action is autorenew_off, not renew, and refuses.
    on_targets = {a.target_id for a in on_cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_RENEWED_THEN_DEAD_AGAIN) not in on_targets

    # A GENUINELY fresh renew, strictly after that off action, re-arms it --
    # accompanied by a fresh, growing-views resync (a real renewal is
    # followed by a real sync), so it's no longer currently dead either
    # (F2): this proves the ordinal gate swings both ways AND that F2 isn't
    # a permanent block once the listing is no longer actually dead.
    _seed_renewal_posture_executed(
        conn,
        capability="listing.renew",
        listing_id=LISTING_RENEWED_THEN_DEAD_AGAIN,
        action_id=f"renew-{LISTING_RENEWED_THEN_DEAD_AGAIN}-2",
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_RENEWED_THEN_DEAD_AGAIN,
        title="Renewed Then Dead Again Print",
        day=TODAY,
        views=20,
        state="active",
        should_auto_renew=False,
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    dead_after_fresh_renew = {d.listing_id for d in analytics.dead_listings(conn, USER_ID, cfg)}
    assert LISTING_RENEWED_THEN_DEAD_AGAIN not in dead_after_fresh_renew

    on_targets_after_fresh_renew = {a.target_id for a in on_cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_RENEWED_THEN_DEAD_AGAIN) in on_targets_after_fresh_renew


def test_on_proposes_a_listing_with_a_real_renew_execution_event(conn):
    """The positive case -- the gate isn't overly restrictive: a listing
    with a real listing.renew execution event, active, auto-renew off, IS
    proposed."""
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_GENUINELY_RENEWED,
        title="Genuinely Renewed Print",
        day=TODAY - timedelta(days=1),
        views=10,
        state="active",
        should_auto_renew=False,
    )
    _seed_renew_executed(conn, listing_id=LISTING_GENUINELY_RENEWED)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg()

    cap = ListingAutorenewOn(FakeEtsyWriteAdapter())
    targets = {a.target_id for a in cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_GENUINELY_RENEWED) in targets


# --- 2. cost ------------------------------------------------------------------


def test_estimated_cost_usd_is_zero(conn):
    _seed_scenario(conn)
    cap = ListingAutorenewOn(FakeEtsyWriteAdapter())

    action = cap.propose(conn, USER_ID, _cfg())[0]

    assert action.estimated_cost_usd == 0.0


# --- 3. execute -----------------------------------------------------------------


def test_execute_calls_update_listing_and_returns_before_after(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_ACTIVE_OFF, should_auto_renew=False, state="active")
    cap = ListingAutorenewOn(fake)
    action = cap.propose(conn, USER_ID, _cfg())[0]

    result = cap.execute(conn, USER_ID, action)

    assert result.before == {"should_auto_renew": False}
    assert result.after == {"should_auto_renew": True}
    assert result.cost_usd == 0.0
    expected_call = (
        "update_listing",
        {"listing_id": LISTING_ACTIVE_OFF, "fields": {"should_auto_renew": True}},
    )
    assert expected_call in fake.calls
    assert fake.listings[LISTING_ACTIVE_OFF]["should_auto_renew"] is True


# --- 4. undo --------------------------------------------------------------------


def test_undo_flips_back_to_off(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_ACTIVE_OFF, should_auto_renew=True, state="active")
    cap = ListingAutorenewOn(fake)
    action = cap.propose(conn, USER_ID, _cfg())[0]

    cap.undo(conn, USER_ID, action)

    assert fake.listings[LISTING_ACTIVE_OFF]["should_auto_renew"] is False
    expected_call = (
        "update_listing",
        {"listing_id": LISTING_ACTIVE_OFF, "fields": {"should_auto_renew": False}},
    )
    assert expected_call in fake.calls


# --- 5. materialize() -- planner-safety grounding ----------------------------


def test_materialize_shares_candidates_with_propose_and_rejects_hallucinated(conn):
    _seed_scenario(conn)
    cap = ListingAutorenewOn(FakeEtsyWriteAdapter())
    cfg = _cfg()

    intent = ProposalIntent(
        capability_key="listing.autorenew_on",
        target_id=str(LISTING_ACTIVE_OFF),
        params={},
        reason="turn auto-renew back on",
    )
    action = cap.materialize(conn, USER_ID, cfg, intent)
    assert action is not None
    assert action.target_id == str(LISTING_ACTIVE_OFF)
    assert action.capability == "listing.autorenew_on"

    for hallucinated in (str(LISTING_ACTIVE_ON), str(LISTING_EXPIRED_OFF), "999999"):
        bad_intent = ProposalIntent(
            capability_key="listing.autorenew_on",
            target_id=hallucinated,
            params={},
            reason="hallucinated target",
        )
        assert cap.materialize(conn, USER_ID, cfg, bad_intent) is None


# --- 6. registration + full run/approve pipeline (policy_verified=True) -----


def test_register_succeeds_capability_has_undo_and_t1_ceiling():
    cap = ListingAutorenewOn(FakeEtsyWriteAdapter())

    register(cap)

    assert REGISTRY["listing.autorenew_on"] is cap
    assert cap.max_tier == Tier.NOTIFY
    assert cap.policy_verified is True
    assert callable(cap.undo)


def test_e2e_run_approve_executes_via_the_full_pipeline(conn):
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_ACTIVE_OFF, should_auto_renew=False, state="active")
    cap = ListingAutorenewOn(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert report.proposed == 1
    assert report.executed == 0  # fresh capability starts at T2/PROPOSE

    action_id = next(
        e.payload["action_id"]
        for e in read_all(conn, "action.proposed")
        if e.payload["capability"] == "listing.autorenew_on"
    )

    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    assert approved.executed == 1
    assert fake.listings[LISTING_ACTIVE_OFF]["should_auto_renew"] is True

    executed_events = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id
    ]
    assert len(executed_events) == 1
    assert executed_events[0].payload["before"] == {"should_auto_renew": False}
    assert executed_events[0].payload["after"] == {"should_auto_renew": True}
    assert executed_events[0].payload["cost_usd"] == 0.0


# --- 7. F1: stale approval must re-validate at execute time -------------------


def test_approve_action_on_a_stale_proposal_fails_instead_of_writing_when_a_newer_off_supersedes_it(
    conn,
):
    """Reviewer's round-4 repro: listing.renew executes -> autorenew_on
    proposes at T2 (stays open, proposal_ttl_days=14, nothing auto-approves
    it) -> autorenew_off executes on the SAME listing (a later,
    contradicting verdict -- the ordinal gate now says autorenew_on should
    NOT fire) -> operator approves the STALE still-open autorenew_on
    proposal. Must fail, not write, because execute() re-derives cfg and
    re-checks _candidates() (F1 fix, renew.py precedent)."""
    _seed_scenario(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_ACTIVE_OFF, should_auto_renew=False, state="active")
    cap = ListingAutorenewOn(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY)
    assert report.proposed == 1
    action_id = next(
        e.payload["action_id"]
        for e in read_all(conn, "action.proposed")
        if e.payload["capability"] == "listing.autorenew_on"
    )

    # A newer, contradicting verdict: autorenew_off executes on the same
    # listing, which flips the ordinal gate against the still-open proposal.
    _seed_renewal_posture_executed(
        conn,
        capability="listing.autorenew_off",
        listing_id=LISTING_ACTIVE_OFF,
        action_id=f"off-{LISTING_ACTIVE_OFF}-stale",
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    approved = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    assert approved.failed == 1
    assert approved.executed == 0
    turned_on_call = (
        "update_listing",
        {"listing_id": LISTING_ACTIVE_OFF, "fields": {"should_auto_renew": True}},
    )
    assert turned_on_call not in fake.calls
    assert fake.listings[LISTING_ACTIVE_OFF]["should_auto_renew"] is False


# --- 8. F2: currently-dead listings are excluded even if freshly renewed ------

LISTING_RENEWED_BUT_STILL_DEAD = 708


def test_propose_excludes_a_renewed_listing_that_is_currently_dead(conn):
    """Operator decision (F2): a listing renewed via a real listing.renew
    execution (ordinal gate says eligible) that ALSO currently meets
    dead_listings()'s criteria (0 sales in-window, flat views,
    sufficiently observed) must NOT be proposed -- avoids the pointless
    on-then-off round trip once autorenew_off catches it next cycle."""
    _seed_renew_executed(conn, listing_id=LISTING_RENEWED_BUT_STILL_DEAD)
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_RENEWED_BUT_STILL_DEAD,
        title="Renewed But Still Dead Print",
        day=TODAY - timedelta(days=100),
        views=5,
        state="active",
        should_auto_renew=False,
    )
    seed_listing_observed_on(
        conn,
        listing_id=LISTING_RENEWED_BUT_STILL_DEAD,
        title="Renewed But Still Dead Print",
        day=TODAY - timedelta(days=1),
        views=5,
        state="active",
        should_auto_renew=False,
    )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = _cfg()

    dead = analytics.dead_listings(conn, USER_ID, cfg)
    assert LISTING_RENEWED_BUT_STILL_DEAD in {d.listing_id for d in dead}

    cap = ListingAutorenewOn(FakeEtsyWriteAdapter())
    targets = {a.target_id for a in cap.propose(conn, USER_ID, cfg)}
    assert str(LISTING_RENEWED_BUT_STILL_DEAD) not in targets


def test_fake_update_listing_applies_should_auto_renew_true_and_records_the_call():
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(998, should_auto_renew=False, state="active")

    updated = fake.update_listing(998, EtsyListingUpdate(should_auto_renew=True))

    assert updated.should_auto_renew is True
    assert fake.listings[998]["should_auto_renew"] is True
    assert ("update_listing", {"listing_id": 998, "fields": {"should_auto_renew": True}}) in (
        fake.calls
    )
