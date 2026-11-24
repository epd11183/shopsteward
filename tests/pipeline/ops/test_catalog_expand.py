"""`listing.catalog_expand` (T11, 2026-08-25 design doc): paced digital
listings from an operator-curated archive folder. No fixture photo files are
committed here (this repo is public) -- every candidate image is generated
programmatically with Pillow."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from shopsteward.adapters.copy.fake import FixtureCopyAdapter
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.adapters.vision.fake import FakeCopyChecker, FixtureCopyChecker
from shopsteward.adapters.vision.interface import CopyCheckResult, CopyCheckVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.listings import asset_store_config, gate3
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.capabilities.catalog_expand import ListingCatalogExpand, _candidates
from shopsteward.pipeline.ops.governor import govern, month_spend
from shopsteward.pipeline.ops.models import ProposedAction, RefusalReason, Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, reject_action, run, undo_action
from shopsteward.pipeline.projections import rebuild_pipeline

from ..listings.helpers import seed_score

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


def _ok() -> CopyCheckResult:
    return CopyCheckResult(verdict=CopyCheckVerdict(ok=True, problems=[]))


def _bad(*problems: str) -> CopyCheckResult:
    return CopyCheckResult(verdict=CopyCheckVerdict(ok=False, problems=list(problems)))


def _cap(adapter, *results) -> ListingCatalogExpand:
    """Live-gates open (precondition_ok) with a queued fake copy checker."""
    return ListingCatalogExpand(
        adapter, live_copy=True, checker=FakeCopyChecker(list(results)), live_check=True
    )


def _score(conn, *paths: Path) -> None:
    # Digital copy needs a vision signal; adopt names the photo from the file hash.
    for p in paths:
        seed_score(conn, f"file-{hashlib.sha256(p.read_bytes()).hexdigest()[:12]}")
    rebuild_pipeline(conn)


class _SuffixedFixtureCopy(FixtureCopyAdapter):
    """The fixture title ends in a "[digest]" marker; real copy ends exactly in
    "(Digital Download)" (listing_copy.txt) -- which the read-back requires."""

    def generate_copy(self, inputs, *, model):
        res = super().generate_copy(inputs, model=model)
        title = re.sub(r" \[[0-9a-f]+\]$", "", res.verdict.title)
        return res.model_copy(update={"verdict": res.verdict.model_copy(update={"title": title})})


@pytest.fixture()
def offline_copy(monkeypatch):
    # The Etsy side is FakeEtsyWriteAdapter (nothing "real" to protect), so the
    # copy adapter is the offline fixture build_drafts would use anyway -- zero
    # live calls (CLAUDE.md) while exercising precondition_ok=True.
    monkeypatch.setattr(
        "shopsteward.pipeline.listings.drafts.build_copy_adapter",
        lambda cfg, *, live: _SuffixedFixtureCopy(),
    )


def _month_spend(conn, cfg) -> float:
    return month_spend(conn, USER_ID, datetime.now(UTC).isoformat()[:7], cfg)


def _brief(conn, cfg):
    rebuild_core(conn)  # `ops brief` precedent: the brief reads core projections
    rebuild_ops(conn)
    return generate_brief(conn, USER_ID, cfg, as_of=TODAY)


def _auto_run(conn, tmp_path, cap):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    photo = _candidate_image(folder, "lighthouse.jpg")
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)
    _score(conn, photo)
    register(cap)
    return cfg, photo


def test_gate_pass_auto_publishes_and_counts_the_fee_once(conn, tmp_path, offline_copy):
    adapter = FakeEtsyWriteAdapter()
    cap = _cap(adapter, _ok())
    cfg, photo = _auto_run(conn, tmp_path, cap)

    (action,) = cap.propose(conn, USER_ID, cfg)
    assert action.capability == "listing.catalog_expand"
    assert action.target_type == "archive_photo"
    assert action.target_id == hashlib.sha256(photo.read_bytes()).hexdigest()
    assert action.estimated_cost_usd == 0.20
    assert action.undo_available is True

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert (report.proposed, report.executed) == (1, 1)  # AUTO: no human step

    (listing_id,) = adapter.listings
    assert adapter.listings[listing_id]["state"] == "active"
    assert [c for c, _ in adapter.calls].count("publish_listing") == 1
    assert gate3.queue(conn, USER_ID) == []  # published, nothing left at Gate 3

    (executed,) = read_all(conn, "action.executed")
    assert executed.payload["cost_usd"] == 0.0
    assert executed.payload["after"]["published"] is True
    (rb,) = read_all(conn, "listingdraft.readback")
    assert rb.payload["ok"] is True and rb.payload["problems"] == []

    # fee counted exactly once (from gate3.published), file never re-proposed
    assert _month_spend(conn, cfg) == cfg.catalog_expansion.listing_fee_usd
    assert cap.propose(conn, USER_ID, cfg) == []
    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert _month_spend(conn, cfg) == cfg.catalog_expansion.listing_fee_usd


def test_copy_problem_leaves_a_draft_surfaced_in_the_brief_and_not_reproposed(
    conn, tmp_path, offline_copy
):
    adapter = FakeEtsyWriteAdapter()
    cap = _cap(adapter, _bad("says 3 elk but photo shows 1 deer"))
    cfg, _ = _auto_run(conn, tmp_path, cap)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert (report.executed, report.failed, report.refused) == (1, 0, 0)

    (listing_id,) = adapter.listings
    assert adapter.listings[listing_id]["state"] == "draft"
    assert "publish_listing" not in [c for c, _ in adapter.calls]
    assert _month_spend(conn, cfg) == 0.0  # never published, never charged

    (executed,) = read_all(conn, "action.executed")
    assert executed.payload["after"]["published"] is False

    brief = _brief(conn, cfg)
    (failure,) = brief.readback_failed
    assert failure.problems == ["says 3 elk but photo shows 1 deer"]
    text = render_text(brief)
    assert "NEEDS YOU (1)" in text and "says 3 elk but photo shows 1 deer" in text

    # not re-proposed, and a second run does not build a second draft
    assert cap.propose(conn, USER_ID, cfg) == []
    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert len(adapter.listings) == 1

    # operator publishes at Gate 3 -> drops out of NEEDS YOU
    gate3.publish(conn, USER_ID, failure.draft_id, adapter)
    assert _brief(conn, cfg).readback_failed == []


def test_structural_problem_blocks_publish(conn, tmp_path, offline_copy):
    class ShortTags(FakeEtsyWriteAdapter):
        def get_listing(self, listing_id):
            d = super().get_listing(listing_id)
            return d.model_copy(update={"tags": d.tags[:5]})

    adapter = ShortTags()
    cap = _cap(adapter, _ok())  # the copy check passes; the structure does not
    cfg, _ = _auto_run(conn, tmp_path, cap)

    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)

    (listing_id,) = adapter.listings
    assert adapter.listings[listing_id]["state"] == "draft"
    (rb,) = read_all(conn, "listingdraft.readback")
    assert rb.payload["ok"] is False
    assert any("5 tags" in p for p in rb.payload["problems"])
    assert len(_brief(conn, cfg).readback_failed) == 1


def test_checker_exception_is_a_problem_not_a_raise(conn, tmp_path, offline_copy):
    adapter = FakeEtsyWriteAdapter()
    cap = _cap(adapter, RuntimeError("vision provider down"))
    cfg, _ = _auto_run(conn, tmp_path, cap)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert report.executed == 1 and report.refused == 0

    (listing_id,) = adapter.listings
    assert adapter.listings[listing_id]["state"] == "draft"
    (rb,) = read_all(conn, "listingdraft.readback")
    assert rb.payload["ok"] is False and "vision provider down" in rb.payload["problems"][0]


def test_fixture_checker_never_publishes(conn, tmp_path, offline_copy):
    adapter = FakeEtsyWriteAdapter()
    # Gates closed: the governor refuses before anything is built.
    closed = ListingCatalogExpand(
        adapter, live_copy=True, checker=FixtureCopyChecker(), live_check=False
    )
    assert closed.precondition_ok is False
    cfg, _ = _auto_run(conn, tmp_path, closed)
    (action,) = closed.propose(conn, USER_ID, cfg)
    assert govern(conn, USER_ID, action, closed, cfg, TODAY).reason == RefusalReason.PRECONDITION
    assert adapter.listings == {}

    # Even if execute() is reached directly, the fixture checker never approves.
    forced = ListingCatalogExpand(
        adapter, live_copy=True, checker=FixtureCopyChecker(), live_check=True
    )
    forced.execute(conn, USER_ID, action)
    (listing_id,) = adapter.listings
    assert adapter.listings[listing_id]["state"] == "draft"
    assert "publish_listing" not in [c for c, _ in adapter.calls]


def test_undo_deactivates_a_published_listing_and_is_a_noop_for_a_draft(
    conn, tmp_path, offline_copy
):
    adapter = FakeEtsyWriteAdapter()
    cap = _cap(adapter, _ok())
    cfg, _ = _auto_run(conn, tmp_path, cap)
    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    (listing_id,) = adapter.listings
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
    assert adapter.listings[listing_id]["state"] == "inactive"
    assert (
        "update_listing_state",
        {"listing_id": listing_id, "state": "inactive"},
    ) in adapter.calls

    # a never-published draft: undo records nothing to reverse on Etsy
    adapter2 = FakeEtsyWriteAdapter()
    cap2 = _cap(adapter2, _bad("wrong species"))
    cap2.undo(
        conn,
        USER_ID,
        ProposedAction(
            action_id="x",
            capability="listing.catalog_expand",
            target_type="archive_photo",
            target_id="f",
            tier=Tier.AUTO,
            reason="r",
            inputs_hash="h",
            estimated_cost_usd=0.2,
            undo_available=True,
            expires_at="2099-01-01",
        ),
    )
    assert [c for c, _ in adapter2.calls] == []


def test_registry_accepts_auto_with_undo():
    cap = _cap(FakeEtsyWriteAdapter())
    assert cap.max_tier == Tier.AUTO and callable(cap.undo)
    register(cap)  # invariant 1: would raise without a callable undo
    assert REGISTRY["listing.catalog_expand"] is cap


# --- the resolution bar (design §6.2 / E16 condition 2) ----------------------


def test_below_min_resolution_is_not_proposed(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    _candidate_image(folder, "too_small.jpg", long_edge=3000)
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)
    cap = _cap(FakeEtsyWriteAdapter())

    assert cap.propose(conn, USER_ID, cfg) == []


# --- precondition: fixture copy must never land on a real listing ----------


def test_precondition_blocks_when_live_copy_is_closed(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    _candidate_image(folder, "lighthouse.jpg")
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)

    cap = ListingCatalogExpand(
        FakeEtsyWriteAdapter(), live_copy=False, checker=FakeCopyChecker([]), live_check=True
    )
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
    cap = _cap(FakeEtsyWriteAdapter())

    run(conn, USER_ID, cfg, [cap], today=TODAY, dry_run=True)
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


def test_pace_exhausted_refuses_never_raises_never_terminalizes(conn, tmp_path, offline_copy):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    photo1 = _candidate_image(folder, "one.jpg")
    photo2 = _candidate_image(folder, "two.jpg", ellipse=True)  # distinct pHash, not a dup
    cfg = _apply_ops_cfg_max_new_per_week(conn, tmp_path, source_folder=folder, max_new_per_week=1)
    # Digital copy needs a vision signal; an unscored file is (rightly) a retryable refusal.
    _score(conn, photo1, photo2)

    adapter = FakeEtsyWriteAdapter()
    cap = _cap(adapter, _ok(), _ok())
    register(cap)

    # propose() truncates to this week's remaining pace (1) even though
    # two eligible candidates exist -- but _candidates() itself is NOT
    # pace-gated (both are still eligible, e.g. for materialize()/execute()).
    assert len(cap.propose(conn, USER_ID, cfg)) == 1
    assert len(_candidates(conn, USER_ID, cfg)) == 2

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert (report.proposed, report.executed) == (1, 1)  # this week's pace (1) is now used
    assert len(adapter.listings) == 1

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


def test_genuine_staleness_still_raises_and_terminalizes(conn, tmp_path, offline_copy):
    """The pace fix must not weaken the OTHER re-validation reasons (file
    removed since propose(), already a landing file, rejected) -- those
    still raise ValueError -> action.failed, exactly as before H1."""
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    photo = _candidate_image(folder, "one.jpg")
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder)
    cap = _cap(FakeEtsyWriteAdapter())
    register(cap)

    run(conn, USER_ID, cfg, [cap], today=TODAY, dry_run=True)
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


def test_unscored_photo_is_a_retryable_refusal_not_an_executed_expansion(
    conn, tmp_path, offline_copy
):
    # No vision score -> copy is skipped -> no title. That must not be recorded as a
    # done expansion (nothing on the ops path would ever title it later).
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    _candidate_image(folder, "one.jpg")
    cfg = _apply_ops_cfg_max_new_per_week(conn, tmp_path, source_folder=folder, max_new_per_week=1)
    cap = _cap(FakeEtsyWriteAdapter())
    register(cap)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert report.executed == 0 and report.refused == 1
    assert read_all(conn, "action.executed") == []
    assert read_all(conn, "action.failed") == []  # retryable, not terminal
