"""`listing.gapfill_reprint` (M8b gap-fill step 2, design §4/§8): only a
PROVEN top seller with an ARCHIVED source and a MISSING POD format is ever
proposed (the reprint ceiling). `execute()` calls `build_pod_reprint` against
the OFFLINE FakePrintFileHost only -- no live adapter is ever constructed,
no network call of any kind. T2/PROPOSE ceiling, never promotable, `undo`
explicitly None -- Gate 3 (the operator's existing `shop build` link step) is
the only reversal, never exercised here."""

import json
from datetime import UTC, datetime

import pytest
from PIL import Image

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.gapfill import ListingGapfillReprint
from shopsteward.pipeline.ops.capabilities.tune_threshold import OpsTuneThreshold
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from shopsteward.pipeline.projections import rebuild_pipeline
from tests.pipeline.ops.helpers import seed_sale_observed

USER_ID = 1
TODAY = datetime.now(UTC).date()

# The shipped catalog's "2:3" (landscape) aspect -- acrylic/poster/canvas all
# match at this long edge (pod test precedent); canvas_portrait does not.
_W, _H = 6000, 4000

LISTING_SELLER = 901  # real sales this window -- proven
LISTING_NON_SELLER = 902  # no sales at all -- must never be proposed


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


def _enable_asset_store(conn, tmp_path, root_name="archive"):
    cfg = asset_store_config.load_asset_store_config().model_dump(by_alias=True)
    cfg["root"] = str(tmp_path / root_name)
    path = tmp_path / f"{root_name}_asset_store.json"
    path.write_text(json.dumps(cfg))
    asset_store_config.apply(conn, USER_ID, path)


def _limit_pod_formats(conn, tmp_path, formats, name="limited_pod.json"):
    edited = pod_config.load_pod_config().model_dump(by_alias=True)
    edited["formats_by_aspect"]["2:3"] = formats
    path = tmp_path / name
    path.write_text(json.dumps(edited))
    pod_config.apply(conn, USER_ID, path)
    rebuild_pod_config(conn)


def _land(conn, tmp_path, *, file_id, photo_id, width=_W, height=_H, fmt="JPEG"):
    path = tmp_path / f"{file_id}.jpg"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(path, "JPEG")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": str(path),
                "base_name": file_id,
                "format": fmt,
                "width": width,
                "height": height,
                "color_space": "sRGB",
                "photo_id": photo_id,
            },
        ),
    )
    rebuild_pipeline(conn)
    return path


def _link_draft_to_listing(conn, *, draft_id: str, listing_id: int) -> None:
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": listing_id,
                "etsy_listing_state": "active",
            },
        ),
    )


def _seed_seller_missing_one_ineligible_format(
    conn, tmp_path, *, listing_id, photo_id, file_id, units=5, linked_format="poster"
):
    """Builds a real (landscape) photo against the SHIPPED default catalog,
    UNCHANGED throughout (no config edits, so pod_config_hash -- and every
    draft_id -- stays constant) -- which builds {acrylic, poster, canvas}
    eagerly (canvas_portrait is orientation-dropped for a landscape photo,
    the one format that stays structurally MISSING). Archives the master,
    links `linked_format`'s draft to `listing_id`, and records `units` real
    sales. Used for the "already has this format -> never re-proposed"
    tests -- canvas_portrait is the honest single candidate; it is never
    actually buildable for this photo (not this fixture's concern -- that is
    exactly the ceiling `execute()` enforces via build_pod_reprint's own
    `not_eligible` result)."""
    _enable_asset_store(conn, tmp_path)
    _land(conn, tmp_path, file_id=file_id, photo_id=photo_id)
    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    assert report.drafts_built == 3  # acrylic, poster, canvas

    draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND format=?",
        (USER_ID, linked_format),
    ).fetchone()["draft_id"]
    _link_draft_to_listing(conn, draft_id=draft_id, listing_id=listing_id)
    rebuild_listings(conn)

    seed_sale_observed(
        conn,
        receipt_id=90000 + listing_id,
        day=TODAY,
        transactions=[(listing_id, 990000 + listing_id, units, 87.00)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    return draft_id


def _seed_seller_with_a_genuinely_buildable_missing_format(
    conn, tmp_path, *, listing_id, photo_id, file_id, units=5, sale_day=None
):
    """Builds a real (landscape) photo against a pod.json NARROWED to
    `poster` only, links that draft to `listing_id`, then WIDENS the catalog
    back to the shipped default (the realistic gap-fill trigger: the
    operator later adds more POD formats) and records `units` real sales.
    Widening changes pod_config_hash -- which every draft_id embeds -- so
    every format (poster included) computes a DIFFERENT draft_id than what
    was actually built; "acrylic"/"canvas" are then both genuinely missing
    AND genuinely eligible (this aspect supports them) for a real
    build_pod_reprint success, which is what these tests exercise."""
    _enable_asset_store(conn, tmp_path)
    _limit_pod_formats(conn, tmp_path, ["poster"])
    _land(conn, tmp_path, file_id=file_id, photo_id=photo_id)
    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    assert report.drafts_built == 1  # poster only

    draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND format='poster'", (USER_ID,)
    ).fetchone()["draft_id"]
    _link_draft_to_listing(conn, draft_id=draft_id, listing_id=listing_id)
    rebuild_listings(conn)

    full = pod_config.load_pod_config().model_dump(by_alias=True)
    full_path = tmp_path / "full_pod.json"
    full_path.write_text(json.dumps(full))
    pod_config.apply(conn, USER_ID, full_path)
    rebuild_pod_config(conn)

    seed_sale_observed(
        conn,
        receipt_id=90000 + listing_id,
        day=sale_day or TODAY,
        transactions=[(listing_id, 990000 + listing_id, units, 87.00)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    return draft_id


def _seed_views_velocity_seller_with_a_genuinely_buildable_missing_format(
    conn, tmp_path, *, listing_id, photo_id, file_id
):
    """M5 (guardrail review, 2026-08-25): same shape as
    `_seed_seller_with_a_genuinely_buildable_missing_format`, but the
    listing is proven ONLY by `proven_listings()`'s views-velocity arm --
    zero sales, ever. This is the arm that newly puts real POD money behind
    a listing with no sales evidence, and the arm whose `reason` text M2
    fixed; no capability-level test drove it through propose() before."""
    from datetime import timedelta

    _enable_asset_store(conn, tmp_path)
    _limit_pod_formats(conn, tmp_path, ["poster"])
    _land(conn, tmp_path, file_id=file_id, photo_id=photo_id)
    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    assert report.drafts_built == 1  # poster only

    draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND format='poster'", (USER_ID,)
    ).fetchone()["draft_id"]
    _link_draft_to_listing(conn, draft_id=draft_id, listing_id=listing_id)
    rebuild_listings(conn)

    full = pod_config.load_pod_config().model_dump(by_alias=True)
    full_path = tmp_path / "full_pod_views.json"
    full_path.write_text(json.dumps(full))
    pod_config.apply(conn, USER_ID, full_path)
    rebuild_pod_config(conn)

    from tests.pipeline.ops.helpers import seed_listing_observed_on

    # delta=10, >= default views_velocity_min_delta=5, zero sales ever.
    for offset, views in ((-29, 10), (0, 20)):
        seed_listing_observed_on(
            conn,
            listing_id=listing_id,
            title=f"Listing {listing_id} (rising, never sold)",
            day=TODAY + timedelta(days=offset),
            views=views,
        )
    rebuild_core(conn)
    rebuild_ops(conn)
    return draft_id


def _cfg():
    return ops_config.load_ops_config()


# --- eligibility (_candidates / propose / materialize) -----------------------


def test_proven_seller_with_archived_source_is_proposed_for_a_missing_format(conn, tmp_path):
    _seed_seller_missing_one_ineligible_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    cap = ListingGapfillReprint(FakePrintFileHost())

    actions = cap.propose(conn, USER_ID, _cfg())

    # acrylic/poster/canvas were all built already -- never re-proposed;
    # canvas_portrait is the one honestly-missing product_type left.
    proposed_types = {a.params["product_type"] for a in actions}
    assert proposed_types == {"canvas_portrait"}
    (action,) = actions
    assert action.capability == "listing.gapfill_reprint"
    assert action.target_type == "listing_reprint"
    assert action.target_id == f"{LISTING_SELLER}:canvas_portrait"
    assert action.params["photo_id"] == "p1"
    assert action.params["listing_id"] == LISTING_SELLER
    assert action.estimated_cost_usd == 0.2  # the Etsy listing fee, reserved by the governor
    assert action.undo_available is True
    assert action.params["draft_id"]
    assert "top seller (5 sold)" in action.reason
    assert "no canvas_portrait yet" in action.reason


def test_a_sale_45_days_ago_the_old_7d_gate_excluded_is_now_proposed(conn, tmp_path):
    """T12 (operator-approved 2026-08-25 /autoplan gate): a listing sold 45
    days ago -- outside the old revenue_window_days=7 gate, inside the new
    proven_window_days=90 -- must be proposed."""
    from datetime import timedelta

    _seed_seller_with_a_genuinely_buildable_missing_format(
        conn,
        tmp_path,
        listing_id=LISTING_SELLER,
        photo_id="p1",
        file_id="f1",
        units=5,
        sale_day=TODAY - timedelta(days=45),
    )
    cap = ListingGapfillReprint(FakePrintFileHost())

    actions = cap.propose(conn, USER_ID, _cfg())

    proposed_types = {a.params["product_type"] for a in actions}
    assert "acrylic" in proposed_types
    (acrylic,) = [a for a in actions if a.params["product_type"] == "acrylic"]
    assert "top seller (5 sold)" in acrylic.reason


def test_views_velocity_zero_sales_listing_is_proposed_with_honest_reason(conn, tmp_path):
    """M5/M2 (guardrail review, 2026-08-25): a listing proven ONLY by the
    T12 views-velocity arm (rising views, zero sales ever) must still be
    proposed for a genuinely missing, eligible format -- but its `reason`
    must never call it "the proven winner" (M2: that lands verbatim on a
    Gate-3 card authorizing a REAL PAID POD SKU)."""
    _seed_views_velocity_seller_with_a_genuinely_buildable_missing_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1"
    )
    cap = ListingGapfillReprint(FakePrintFileHost())

    actions = cap.propose(conn, USER_ID, _cfg())

    proposed_types = {a.params["product_type"] for a in actions}
    assert "acrylic" in proposed_types
    (acrylic,) = [a for a in actions if a.params["product_type"] == "acrylic"]
    assert "rising views, no sales yet" in acrylic.reason
    assert "proven winner" not in acrylic.reason  # M2 -- never for a zero-sales listing
    assert "top seller" not in acrylic.reason


def test_top_seller_reason_still_calls_it_the_proven_winner(conn, tmp_path):
    """M2's positive case: a real sales-proof listing still gets the
    original, accurate phrasing."""
    _seed_seller_with_a_genuinely_buildable_missing_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    cap = ListingGapfillReprint(FakePrintFileHost())

    actions = cap.propose(conn, USER_ID, _cfg())

    (acrylic,) = [a for a in actions if a.params["product_type"] == "acrylic"]
    assert "top seller (5 sold)" in acrylic.reason
    assert "reprint the proven winner." in acrylic.reason


def test_top_seller_with_no_archived_source_is_not_reprintable(conn, tmp_path):
    # Sales exist, but the source was never archived (asset store never
    # enabled) -- resolve_source finds no draft/photo linkage at all.
    pod_config.seed(conn, USER_ID)
    rebuild_pod_config(conn)
    rebuild_listings(conn)
    seed_sale_observed(
        conn,
        receipt_id=1,
        day=TODAY,
        transactions=[(LISTING_SELLER, 1001, 3, 87.00)],
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    cap = ListingGapfillReprint(FakePrintFileHost())

    assert cap.propose(conn, USER_ID, _cfg()) == []


def test_non_seller_is_never_proposed(conn, tmp_path):
    _seed_seller_missing_one_ineligible_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    cap = ListingGapfillReprint(FakePrintFileHost())

    actions = cap.propose(conn, USER_ID, _cfg())

    assert all(a.params["listing_id"] != LISTING_NON_SELLER for a in actions)


def test_a_format_the_photo_already_has_is_never_reproposed(conn, tmp_path):
    _seed_seller_missing_one_ineligible_format(
        conn,
        tmp_path,
        listing_id=LISTING_SELLER,
        photo_id="p1",
        file_id="f1",
        units=5,
        linked_format="poster",
    )
    cap = ListingGapfillReprint(FakePrintFileHost())

    actions = cap.propose(conn, USER_ID, _cfg())

    proposed_types = {a.params["product_type"] for a in actions}
    assert "poster" not in proposed_types
    assert "acrylic" not in proposed_types
    assert "canvas" not in proposed_types


def test_propose_and_materialize_share_one_grounding_function(conn, tmp_path):
    _seed_seller_with_a_genuinely_buildable_missing_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    cap = ListingGapfillReprint(FakePrintFileHost())
    (proposed,) = [
        a for a in cap.propose(conn, USER_ID, _cfg()) if a.params["product_type"] == "acrylic"
    ]

    intent = ProposalIntent(
        capability_key="listing.gapfill_reprint",
        target_id=proposed.target_id,
        params={},
        reason="test",
    )
    materialized = cap.materialize(conn, USER_ID, _cfg(), intent)

    assert materialized == proposed


def test_a_hallucinated_target_is_ungrounded(conn, tmp_path):
    _seed_seller_missing_one_ineligible_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    cap = ListingGapfillReprint(FakePrintFileHost())

    intent = ProposalIntent(
        capability_key="listing.gapfill_reprint",
        target_id=f"{LISTING_SELLER}:not_a_real_format",
        params={},
        reason="test",
    )
    assert cap.materialize(conn, USER_ID, _cfg(), intent) is None

    # A real target_id but for a listing that was never a proven seller at
    # all (or a format it already has) is equally ungrounded.
    intent2 = ProposalIntent(
        capability_key="listing.gapfill_reprint",
        target_id="999999:acrylic",
        params={},
        reason="test",
    )
    assert cap.materialize(conn, USER_ID, _cfg(), intent2) is None

    intent3 = ProposalIntent(
        capability_key="listing.gapfill_reprint",
        target_id=f"{LISTING_SELLER}:poster",
        params={},
        reason="test",
    )
    assert cap.materialize(conn, USER_ID, _cfg(), intent3) is None


# --- registration --------------------------------------------------------


def test_registers_at_auto_with_a_real_undo_and_policy_unverified():
    cap = ListingGapfillReprint(FakePrintFileHost())

    register(cap)

    assert REGISTRY["listing.gapfill_reprint"] is cap
    assert cap.max_tier == Tier.AUTO
    assert callable(cap.undo)
    # Stays False until policy row E17 exists (the PR that adds it flips it).
    assert cap.policy_verified is False
    # Offline construction (no adapters, no live gates): the governor refuses.
    assert cap.precondition_ok is False


def test_estimate_cost_is_the_injected_listing_fee_even_for_old_zero_cost_proposals(conn, tmp_path):
    _seed_seller_with_a_genuinely_buildable_missing_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    cap = ListingGapfillReprint(FakePrintFileHost(), listing_fee_usd=0.2)
    (action,) = [
        a for a in cap.propose(conn, USER_ID, _cfg()) if a.params["product_type"] == "acrylic"
    ]
    stored = action.model_copy(update={"estimated_cost_usd": 0.0})  # minted before the fee

    assert cap.estimate_cost_usd(stored) == 0.2


# --- e2e via the runner: T2, offline execute, no duplicate draft ------------


def test_planner_gate_still_drops_unknown_policy_ungrounded_with_gapfill_registered(conn, tmp_path):
    _seed_seller_missing_one_ineligible_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    gapfill = ListingGapfillReprint(FakePrintFileHost())
    gapfill.policy_verified = True  # past POLICY_UNVERIFIED so the grounding drop is the one tested
    register(gapfill)
    register(OpsTuneThreshold())
    cfg = _cfg()
    cfg.autonomy.planner_max_per_capability_per_run = 5

    from shopsteward.adapters.planner.fake import FakePlannerAdapter
    from shopsteward.pipeline.ops.planner import plan_proposals

    intents = [
        ProposalIntent(
            capability_key="not.a.real.capability", target_id="whatever", params={}, reason="test"
        ),
        ProposalIntent(
            capability_key="listing.gapfill_reprint",
            target_id="999999:acrylic",
            params={},
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
    # E10: split "ungrounded" -- 999999 was never a real listing_id gapfill_
    # reprint's own propose() would ever surface, so this is a genuinely
    # hallucinated target, not a stale one.
    assert "hallucinated_target" in reasons


# --- no secrets / no network -------------------------------------------------


# --- undo on a no-undo capability must never crash (runner.py fix) ---------


def test_undo_on_a_capability_with_a_real_undo_still_works(conn):
    """Unaffected-by-the-guard regression: a capability whose `undo` IS
    callable must undo exactly as before (ops.tune_threshold precedent,
    test_tune_threshold.py's own e2e test)."""
    from datetime import timedelta

    from tests.pipeline.ops.helpers import seed_listing_observed_on

    title = "Listing 701"
    seed_listing_observed_on(
        conn, listing_id=701, title=title, day=TODAY - timedelta(days=40), views=10
    )
    seed_listing_observed_on(
        conn, listing_id=701, title=title, day=TODAY - timedelta(days=1), views=15
    )
    rebuild_core(conn)
    rebuild_ops(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cap = OpsTuneThreshold()
    cfg = _cfg()
    cfg.autonomy.enabled = True
    cfg.autonomy.weekly_catalog_pct_cap = 1.0

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY)

    undo_action(conn, USER_ID, action_id, [cap])

    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id]
    assert len(undone) == 1
