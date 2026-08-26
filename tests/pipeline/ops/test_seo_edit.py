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

import json
from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.seo_edit import ListingSeoEdit, _eligible
from shopsteward.pipeline.ops.keyword_probe import KeywordProbeAggregates, KeywordProbeResult
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
        conn,
        LISTING_DIGITAL,
        "Loon Digital Download",
        views=cfg.seo_edit.min_lifetime_views - 1,
        tags=["loon"],  # non-empty -- isolates the views gate from the zero-tags branch
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), title="X")) is None


def test_a_sale_in_the_revenue_window_is_not_eligible(conn):
    from tests.pipeline.ops.helpers import seed_sale_observed

    _seed_listing(conn, LISTING_DIGITAL, "Loon Digital Download", tags=["loon"])
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


# --- eligibility: zero-tag active listings (operator report, 2026-08) ------

LISTING_ZERO_TAGS = 941  # active, 0 tags, 1 view -- eligible regardless of views


def test_zero_tag_active_listing_is_eligible_regardless_of_low_views(conn):
    _seed_listing(conn, LISTING_ZERO_TAGS, "Untagged Print", views=1, tags=[])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_ZERO_TAGS), title="Untagged Print Restyled")
    )
    assert action is not None
    assert action.target_id == str(LISTING_ZERO_TAGS)


def test_zero_tag_listing_that_also_qualifies_under_views_is_not_double_counted(conn):
    cfg = _cfg()
    _seed_listing(
        conn, LISTING_ZERO_TAGS, "Untagged Print", views=cfg.seo_edit.min_lifetime_views, tags=[]
    )
    rebuild_core(conn)
    rebuild_ops(conn)

    from shopsteward.pipeline.ops.capabilities.seo_edit import _eligible

    targets = _eligible(conn, USER_ID, cfg)
    assert list(targets).count(str(LISTING_ZERO_TAGS)) == 1
    # A dict key can never repeat, so the count above can't actually catch a
    # missing de-dup guard -- assert the branch-1 (zero-tags-first) ordering
    # directly: this fails if branch order is ever inverted.
    assert "zero tags" in targets[str(LISTING_ZERO_TAGS)].reason


def test_a_few_tags_is_not_eligible_under_the_default_zero_threshold(conn):
    cfg = _cfg()
    assert cfg.seo_edit.min_tags_before_flagged == 0
    _seed_listing(conn, LISTING_ZERO_TAGS, "Lightly Tagged Print", views=1, tags=["a", "b"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, cfg, _intent(str(LISTING_ZERO_TAGS), title="X")) is None


def test_e2e_zero_tag_listing_materialize_execute_round_trip(conn):
    _seed_listing(conn, LISTING_ZERO_TAGS, "Untagged Print", views=1, tags=[])
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_ZERO_TAGS, state="active", title="Untagged Print", tags=[])
    cap = ListingSeoEdit(fake)
    cfg = _cfg(enabled=True, weekly_catalog_pct_cap=1.0)

    action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        _intent(
            str(LISTING_ZERO_TAGS),
            title="Untagged Print Fine Art",
            tags=["loon", "lake", "art"],
        ),
    )
    assert action is not None

    run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approved = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved.executed == 1

    update_calls = [c for c in fake.calls if c[0] == "update_listing"]
    assert len(update_calls) == 1
    assert update_calls[0][1]["fields"] == {
        "title": "Untagged Print Fine Art",
        "tags": ["loon", "lake", "art"],
    }
    executed_event = [
        e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id
    ][0]
    assert executed_event.payload["before"]["tags"] == []


# --- eligibility: tag MISALIGNMENT (keyword-probe backed, 2026-08-25) ------
#
# Anchored on the two real live listings that motivated this branch:
# 4464098863 "Majestic Bull Elk Wall Art" (13 tags, 9/13 overlap the
# ranker-rewarded set -- well optimized, must NOT be flagged) and 4465118874
# "Garden of the Gods Print | Colorado Red Rock Landscape" (13 tags, 3/13
# overlap -- genuinely misaligned, MUST be flagged).

LISTING_MISALIGN = 951

_RANKER_TAGS_9 = [
    "elk wall art",
    "elk photograph",
    "wildlife wall art",
    "rocky mountain print",
    "nature photography",
    "cabin wall decor",
    "elk print",
    "wildlife photograph",
    "mountain wall art",
]


def _seed_probe(conn, *, phrase: str, tag_frequency: dict, created_at: datetime) -> None:
    result = KeywordProbeResult(
        phrase=phrase,
        top_n=25,
        competition_count=100,
        aggregates=KeywordProbeAggregates(
            sample_size=sum(tag_frequency.values()) or 1,
            tag_frequency=tag_frequency,
            median_price_usd=30.0,
            min_price_usd=20.0,
            max_price_usd=40.0,
            median_favorites_per_day=1.0,
            min_favorites_per_day=0.5,
            max_favorites_per_day=2.0,
        ),
    )
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (
            USER_ID,
            "etsy.keyword.probed",
            json.dumps(result.model_dump()),
            created_at.isoformat().replace("+00:00", "Z"),
        ),
    )
    conn.commit()


def test_well_aligned_tagged_listing_is_not_flagged_misaligned(conn):
    """9/13 overlap (the real "Majestic Bull Elk Wall Art" case) -- well
    optimized, its problem is shop authority (0 views), not tags. Must not
    be flagged by ANY branch."""
    now = datetime.now(UTC)
    tags = _RANKER_TAGS_9 + ["16x20 print", "home decor", "gift for him", "photography"]
    assert len(tags) == 13
    _seed_listing(
        conn, LISTING_MISALIGN, "Majestic Bull Elk Wall Art Photograph", views=0, tags=tags
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    _seed_probe(
        conn,
        phrase="elk wall art",
        tag_frequency={t: 5 for t in _RANKER_TAGS_9},
        created_at=now - timedelta(days=1),
    )
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_MISALIGN), title="X")) is None


def test_misaligned_tagged_listing_is_flagged_with_specific_reason(conn):
    """3/13 overlap (the real "Garden of the Gods" case) -- hyper-specific
    tags while the ranker rewards generic regional terms. Must be flagged,
    with an operator-facing reason naming the overlap, and must never claim
    a never-sold listing is a top seller."""
    now = datetime.now(UTC)
    overlap_tags = ["colorado wall art", "colorado landscape", "colorado print"]
    hyper_specific = [
        "garden rocks print",
        "rock formation art",
        "red rock canyon",
        "garden of the gods",
        "colorado springs print",
        "sandstone formation",
        "pikes peak view",
        "colorado hiking print",
        "red rock landscape",
        "colorado geology art",
    ]
    tags = overlap_tags + hyper_specific
    assert len(tags) == 13
    _seed_listing(
        conn,
        LISTING_MISALIGN,
        "Garden of the Gods Print Colorado Red Rock Landscape",
        views=0,
        tags=tags,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    _seed_probe(
        conn,
        phrase="colorado landscape print",
        tag_frequency={
            "colorado wall art": 9,
            "colorado landscape": 7,
            "colorado print": 6,
            "mountain wall art": 3,
        },
        created_at=now - timedelta(days=1),
    )
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_MISALIGN), title="Realigned Title")
    )
    assert action is not None
    assert action.target_id == str(LISTING_MISALIGN)
    assert "3 of 13 tags match what Etsy's ranker rewards" in action.reason
    assert "colorado landscape print" in action.reason
    assert "top seller" not in action.reason.lower()
    assert "best seller" not in action.reason.lower()


def test_no_probe_coverage_is_not_flagged_misaligned(conn):
    """Absence is not zero -- a listing with no fresh probe evidence at all
    must never be flagged as misaligned, even if it has plenty of tags."""
    _seed_listing(
        conn,
        LISTING_MISALIGN,
        "Never Probed Print",
        views=0,
        tags=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())

    assert cap.materialize(conn, USER_ID, _cfg(), _intent(str(LISTING_MISALIGN), title="X")) is None


def test_zero_tag_listing_with_a_matching_probe_is_still_claimed_by_the_zero_tag_branch(conn):
    """A zero-tag listing trivially has 0 overlap (0 <= any threshold), but
    the zero-tags branch is checked FIRST and must keep the more urgent,
    clearer message -- never silently swapped for the misalignment one."""
    now = datetime.now(UTC)
    _seed_listing(conn, LISTING_MISALIGN, "Elk Wall Art Print", views=0, tags=[])
    rebuild_core(conn)
    rebuild_ops(conn)
    _seed_probe(
        conn,
        phrase="elk wall art",
        tag_frequency={t: 5 for t in _RANKER_TAGS_9},
        created_at=now - timedelta(days=1),
    )
    cfg = _cfg()

    targets = _eligible(conn, USER_ID, cfg)
    assert list(targets).count(str(LISTING_MISALIGN)) == 1
    assert "zero tags" in targets[str(LISTING_MISALIGN)].reason


def test_listing_claimed_by_the_refresh_branch_is_not_double_claimed_by_misalignment(conn):
    """A listing with enough views to qualify for the refresh (views) branch
    keeps that reason -- the misalignment branch must never steal it, even
    though its tags would also qualify as misaligned."""
    cfg = _cfg()
    now = datetime.now(UTC)
    overlap_tags = ["colorado wall art", "colorado landscape", "colorado print"]
    hyper_specific = [f"unique tag {i}" for i in range(10)]
    tags = overlap_tags + hyper_specific
    _seed_listing(
        conn,
        LISTING_MISALIGN,
        "Garden of the Gods Print Colorado Red Rock Landscape",
        views=cfg.seo_edit.min_lifetime_views,
        tags=tags,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    _seed_probe(
        conn,
        phrase="colorado landscape print",
        tag_frequency={"colorado wall art": 9, "colorado landscape": 7, "colorado print": 6},
        created_at=now - timedelta(days=1),
    )

    targets = _eligible(conn, USER_ID, cfg)
    assert list(targets).count(str(LISTING_MISALIGN)) == 1
    assert "refresh title/tags." in targets[str(LISTING_MISALIGN)].reason
    assert "ranker rewards" not in targets[str(LISTING_MISALIGN)].reason


def test_overlap_threshold_boundary(conn):
    """Overlap exactly AT the configured threshold is flagged; one more
    overlapping tag than the threshold is not."""
    cfg = _cfg()
    threshold = cfg.seo_edit.min_ranker_tag_overlap
    now = datetime.now(UTC)
    ranker_pool = [f"ranker tag {i}" for i in range(threshold + 1)]

    def _build(overlap_n: int, listing_id: int) -> None:
        overlap_tags = ranker_pool[:overlap_n]
        filler = [f"unique tag {listing_id} {i}" for i in range(13 - overlap_n)]
        _seed_listing(
            conn,
            listing_id,
            "Boundary Test Wall Art Print",
            views=0,
            tags=overlap_tags + filler,
        )

    _build(threshold, 960)  # at the threshold -- flagged
    _build(threshold + 1, 961)  # one more -- not flagged
    rebuild_core(conn)
    rebuild_ops(conn)
    _seed_probe(
        conn,
        phrase="boundary test wall art",
        tag_frequency={t: 5 for t in ranker_pool},
        created_at=now - timedelta(days=1),
    )

    targets = _eligible(conn, USER_ID, cfg)
    assert "960" in targets
    assert "961" not in targets


def test_misalignment_branch_deterministic_for_a_fixed_as_of(conn):
    fixed_as_of = datetime(2026, 8, 25, tzinfo=UTC)
    overlap_tags = ["colorado wall art", "colorado landscape", "colorado print"]
    hyper_specific = [f"unique tag {i}" for i in range(10)]
    _seed_listing(
        conn,
        LISTING_MISALIGN,
        "Garden of the Gods Print Colorado Red Rock Landscape",
        views=0,
        tags=overlap_tags + hyper_specific,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    _seed_probe(
        conn,
        phrase="colorado landscape print",
        tag_frequency={"colorado wall art": 9, "colorado landscape": 7, "colorado print": 6},
        created_at=fixed_as_of - timedelta(days=1),
    )
    cfg = _cfg()

    first = _eligible(conn, USER_ID, cfg, as_of=fixed_as_of)
    second = _eligible(conn, USER_ID, cfg, as_of=fixed_as_of)
    assert first[str(LISTING_MISALIGN)] == second[str(LISTING_MISALIGN)]


def test_stale_probe_between_propose_and_approve_does_not_terminalize(conn):
    """A probe merely aging out between materialize()/propose and
    execute()/approve must NOT terminalize an otherwise-still-valid action
    (StaleTargetError) -- probe freshness is a research-cache artifact, not
    a fact about the target. The action was validly proposed while the
    probe was fresh; execute() must still succeed even though, by the time
    it runs, the probe is stale by wall-clock `now()`."""
    now = datetime.now(UTC)
    overlap_tags = ["colorado wall art", "colorado landscape", "colorado print"]
    hyper_specific = [f"unique tag {i}" for i in range(10)]
    tags = overlap_tags + hyper_specific
    _seed_listing(
        conn,
        LISTING_MISALIGN,
        "Garden of the Gods Print Colorado Red Rock Landscape",
        views=0,
        tags=tags,
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    # The probe is STALE right now (default max_age_days=90) -- it was only
    # ever fresh in the past, simulating "it aged out since the action was
    # proposed".
    _seed_probe(
        conn,
        phrase="colorado landscape print",
        tag_frequency={"colorado wall art": 9, "colorado landscape": 7, "colorado print": 6},
        created_at=now - timedelta(days=91),
    )
    cfg = ops_config.load_ops_config()

    # Sanity: with the probe stale, strict _eligible() (materialize()'s own
    # path) no longer offers this target at all.
    assert str(LISTING_MISALIGN) not in _eligible(conn, USER_ID, cfg)

    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(
        LISTING_MISALIGN,
        state="active",
        title="Garden of the Gods Print Colorado Red Rock Landscape",
        tags=tags,
    )
    cap = ListingSeoEdit(fake)
    forged = ProposedAction(
        action_id="forged-stale-probe",
        capability="listing.seo_edit",
        target_type="listing",
        target_id=str(LISTING_MISALIGN),
        tier=Tier.PROPOSE,
        reason="realign tags",
        inputs_hash="irrelevant",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(TODAY + timedelta(days=14)).isoformat(),
        params={"tags": ["colorado wall art", "colorado landscape", "colorado print", "new tag"]},
    )

    result = cap.execute(conn, USER_ID, forged)  # must NOT raise StaleTargetError
    assert result.after == {
        "tags": ["colorado wall art", "colorado landscape", "colorado print", "new tag"]
    }
    update_calls = [c for c in fake.calls if c[0] == "update_listing"]
    assert len(update_calls) == 1


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


def test_materialize_drops_an_llm_authored_misrepresentation_tag(conn):
    """M2 (guardrail review 2026-08-25): `_validate_params` runs
    `keyword_probe._is_safe_ranker_tag` over `tags` too -- nothing stops the
    LLM COMPOSING "bison painting" itself (not copied from a probe fact);
    the guard must catch that regardless of provenance. Rejects the WHOLE
    tags update (never silently drops just the one bad tag) -- see
    `_validate_params`'s own docstring for why."""
    _seed_listing(conn, LISTING_DIGITAL, "Bison Wall Art Photograph", tags=["bison"])
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingSeoEdit(FakeEtsyWriteAdapter())
    cfg = _cfg()

    action = cap.materialize(
        conn,
        USER_ID,
        cfg,
        _intent(str(LISTING_DIGITAL), tags=["western wall art", "bison painting"]),
    )
    assert action is None  # the whole tags update is dropped, "bison painting" never reaches Etsy

    # the same intent, minus the offending tag, is accepted -- proves the
    # guard is on the CONTENT, not a blanket refusal of this target/target_id.
    ok_action = cap.materialize(
        conn, USER_ID, cfg, _intent(str(LISTING_DIGITAL), tags=["western wall art"])
    )
    assert ok_action is not None
    assert ok_action.params == {"tags": ["western wall art"]}


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
        conn, USER_ID, event.payload["action_id"], [cap], cfg=cfg, today=TODAY, live_autonomy=True
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

    approved = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
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

    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
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

    approved = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
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

    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
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
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=later, live_autonomy=True)

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
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True)
    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)

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
