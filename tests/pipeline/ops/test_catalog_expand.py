"""`listing.catalog_expand` (T11, 2026-08-25 design doc): paced digital
listings from an operator-curated archive folder. No fixture photo files are
committed here (this repo is public) -- every candidate image is generated
programmatically with Pillow."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from shopsteward.adapters.copy.fake import FixtureCopyAdapter
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import asset_store_config, gate3
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.catalog_expand import ListingCatalogExpand, _candidates
from shopsteward.pipeline.ops.governor import govern
from shopsteward.pipeline.ops.models import RefusalReason, Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, reject_action, run

USER_ID = 1
TODAY = datetime.now(UTC).date()


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Isolate mockups output + the operator templates dir so this test only
    # ever touches tmp_path (never real data/) -- test_shop_build.py precedent.
    monkeypatch.setenv("SHOPSTEWARD_MOCKUPS_DIR", str(tmp_path / "mockups"))
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "no_such_operator_dir"))
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _candidate_image(
    folder: Path, name: str, *, long_edge: int = 6000, ellipse: bool = False
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    img = Image.new("RGB", (long_edge, long_edge), (10, 40, 90))
    if ellipse:
        # photo_match.py's own precedent: a flat color has no distinguishing
        # DCT coefficients (every AC value ties the median), so two
        # otherwise-identical solid-color images pHash-collide as
        # near-duplicates. A second, genuinely distinct candidate needs
        # actual texture.
        from PIL import ImageDraw

        ImageDraw.Draw(img).ellipse((0, 0, long_edge // 2, long_edge // 2), fill=(200, 40, 10))
    img.save(path, "JPEG")
    return path


def _enable_asset_store(conn, tmp_path) -> None:
    """gapfill.py test precedent -- point the archive root at tmp_path so
    archive_master() never writes under the real data/ dir."""
    cfg = asset_store_config.load_asset_store_config().model_dump(by_alias=True)
    cfg["root"] = str(tmp_path / "archive")
    path = tmp_path / "asset_store.json"
    path.write_text(json.dumps(cfg))
    asset_store_config.apply(conn, USER_ID, path)


def _apply_ops_cfg(conn, tmp_path, *, source_folder: Path, min_long_edge_px: int = 6000):
    """pod/config.py `_limit_pod_formats` precedent: edit the shipped
    ops.json, apply it, and hand back the DB-backed OpsConfig -- execute()
    re-reads config from the DB (get_ops_config), so a cfg object built by
    hand and never applied would silently diverge from what execute() sees."""
    cfg = ops_config.load_ops_config().model_dump(by_alias=True)
    cfg["autonomy"]["enabled"] = True
    cfg["autonomy"]["weekly_catalog_pct_cap"] = 1.0
    cfg["catalog_expansion"]["source_folder"] = str(source_folder)
    cfg["catalog_expansion"]["min_long_edge_px"] = min_long_edge_px
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(cfg))
    ops_config.apply(conn, USER_ID, path)
    rebuild_ops(conn)
    return ops_config.get_ops_config(conn, USER_ID)


# --- the smallest test that proves it (design §9) ---------------------------


def test_propose_approve_pushes_a_gate3_card_then_stops_reproposing(conn, tmp_path, monkeypatch):
    # precondition_ok requires live_copy=True (module docstring: the single
    # control preventing fixture copy from landing on a REAL listing) --
    # here the Etsy side is FakeEtsyWriteAdapter (nothing "real" to protect),
    # so the copy adapter is patched to the same offline FixtureCopyAdapter
    # build_drafts would use anyway, keeping this test at zero live calls
    # (CLAUDE.md: no live external API calls from any test) while still
    # exercising the real precondition_ok=True code path.
    monkeypatch.setattr(
        "shopsteward.pipeline.listings.drafts.build_copy_adapter",
        lambda cfg, *, live: FixtureCopyAdapter(),
    )
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    photo_path = _candidate_image(folder, "lighthouse.jpg")
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)

    adapter = FakeEtsyWriteAdapter()
    cap = ListingCatalogExpand(adapter, live_copy=True)
    register(cap)

    # 1. propose() -> exactly one action, target_id/cost/tier as designed.
    actions = cap.propose(conn, USER_ID, cfg)
    assert len(actions) == 1
    (action,) = actions
    assert action.capability == "listing.catalog_expand"
    assert action.target_type == "archive_photo"
    assert action.target_id == hashlib.sha256(photo_path.read_bytes()).hexdigest()
    assert action.estimated_cost_usd == 0.20
    assert action.tier == Tier.PROPOSE

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert report.proposed == 1
    assert report.executed == 0  # T2 ceiling -- never auto-executed

    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    # 2. approve_action() against a FakeEtsyWriteAdapter -> one pushed Gate-3
    # card with real title/price/etsy_listing_id.
    approved = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved.executed == 1

    cards = gate3.queue(conn, USER_ID)
    assert len(cards) == 1
    (card,) = cards
    assert card.state == "pushed"
    assert card.etsy_listing_id is not None
    assert card.title is not None
    assert card.price is not None

    # 3. propose() again -> [] -- the file is now a landing file.
    assert cap.propose(conn, USER_ID, cfg) == []


# --- the resolution bar (design §6.2 / E16 condition 2) ----------------------


def test_below_min_resolution_is_not_proposed(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    _candidate_image(folder, "too_small.jpg", long_edge=3000)
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)
    cap = ListingCatalogExpand(FakeEtsyWriteAdapter(), live_copy=True)

    assert cap.propose(conn, USER_ID, cfg) == []


# --- precondition: fixture copy must never land on a real listing ----------


def test_precondition_blocks_when_live_copy_is_closed(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    _candidate_image(folder, "lighthouse.jpg")
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)

    cap = ListingCatalogExpand(FakeEtsyWriteAdapter(), live_copy=False)
    assert cap.precondition_ok is False

    (action,) = cap.propose(conn, USER_ID, cfg)
    decision = govern(conn, USER_ID, action, cap, cfg, TODAY)

    assert decision.approved is False
    assert decision.reason == RefusalReason.PRECONDITION


# --- rejection is the normal verdict (design §7, load-bearing) -------------


def test_rejected_photo_does_not_return_tomorrow(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    _candidate_image(folder, "lighthouse.jpg")
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)
    cap = ListingCatalogExpand(FakeEtsyWriteAdapter(), live_copy=True)

    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    reject_action(conn, USER_ID, action_id)

    assert cap.propose(conn, USER_ID, cfg) == []


# --- H1 (guardrail review, 2026-08-25): pace is a governor REFUSAL, never
# an execute()-time ValueError -- an over-pace approval must never
# terminalize the action_id. ------------------------------------------------


def _apply_ops_cfg_max_new_per_week(conn, tmp_path, *, source_folder, max_new_per_week: int):
    cfg = ops_config.load_ops_config().model_dump(by_alias=True)
    cfg["autonomy"]["enabled"] = True
    cfg["autonomy"]["weekly_catalog_pct_cap"] = 1.0
    cfg["catalog_expansion"]["source_folder"] = str(source_folder)
    cfg["catalog_expansion"]["max_new_per_week"] = max_new_per_week
    path = tmp_path / "ops.json"
    path.write_text(json.dumps(cfg))
    ops_config.apply(conn, USER_ID, path)
    rebuild_ops(conn)
    return ops_config.get_ops_config(conn, USER_ID)


def test_pace_exhausted_refuses_never_raises_never_terminalizes(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "shopsteward.pipeline.listings.drafts.build_copy_adapter",
        lambda cfg, *, live: FixtureCopyAdapter(),
    )
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    _candidate_image(folder, "one.jpg")
    photo2 = _candidate_image(folder, "two.jpg", ellipse=True)  # distinct pHash, not a dup
    cfg = _apply_ops_cfg_max_new_per_week(conn, tmp_path, source_folder=folder, max_new_per_week=1)

    adapter = FakeEtsyWriteAdapter()
    cap = ListingCatalogExpand(adapter, live_copy=True)
    register(cap)

    # propose() truncates to this week's remaining pace (1) even though
    # two eligible candidates exist -- but _candidates() itself is NOT
    # pace-gated (both are still eligible, e.g. for materialize()/execute()).
    assert len(cap.propose(conn, USER_ID, cfg)) == 1
    assert len(_candidates(conn, USER_ID, cfg)) == 2

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert report.proposed == 1
    first_action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approved1 = approve_action(
        conn, USER_ID, first_action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved1.executed == 1  # this week's pace (1) is now fully used

    # A second action for the OTHER photo reaches "proposed" anyway (e.g.
    # the planner path -- materialize() is deliberately not pace-gated).
    file_id2 = hashlib.sha256(photo2.read_bytes()).hexdigest()
    intent = ProposalIntent(capability_key=cap.key, target_id=file_id2, reason="second candidate")
    action2 = cap.materialize(conn, USER_ID, cfg, intent)
    assert action2 is not None
    append(conn, Event(user_id=USER_ID, type="action.proposed", payload=action2.model_dump()))

    decision = govern(conn, USER_ID, action2, cap, cfg, TODAY)
    assert decision.approved is False
    assert decision.reason == RefusalReason.PACE

    approved2 = approve_action(
        conn, USER_ID, action2.action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved2.refused == 1
    assert approved2.executed == 0  # weekly pace genuinely still limits executions

    # The crux of H1: never a terminal state for a pace-only refusal.
    statuses = {
        e.type for e in read_all(conn, "action.") if e.payload.get("action_id") == action2.action_id
    }
    assert "action.failed" not in statuses
    assert "action.expired" not in statuses
    assert "action.rejected" not in statuses

    # And it is still approvable later, once the pace resets (a new ISO week).
    next_week = TODAY + timedelta(days=7)
    approved3 = approve_action(
        conn, USER_ID, action2.action_id, [cap], cfg=cfg, today=next_week, live_autonomy=True
    )
    assert approved3.executed == 1


def test_genuine_staleness_still_raises_and_terminalizes(conn, tmp_path, monkeypatch):
    """The pace fix must not weaken the OTHER re-validation reasons (file
    removed since propose(), already a landing file, rejected) -- those
    still raise ValueError -> action.failed, exactly as before H1."""
    monkeypatch.setattr(
        "shopsteward.pipeline.listings.drafts.build_copy_adapter",
        lambda cfg, *, live: FixtureCopyAdapter(),
    )
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    photo = _candidate_image(folder, "one.jpg")
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)
    cap = ListingCatalogExpand(FakeEtsyWriteAdapter(), live_copy=True)
    register(cap)

    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    photo.unlink()  # genuinely stale -- not a pace reason

    approved = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved.failed == 1
    statuses = {
        e.type for e in read_all(conn, "action.") if e.payload.get("action_id") == action_id
    }
    assert "action.failed" in statuses
