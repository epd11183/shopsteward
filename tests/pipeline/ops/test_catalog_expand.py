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
from shopsteward.adapters.vision.fake import (
    FakeCopyChecker,
    FakeVisionAdapter,
    FixtureCopyChecker,
    FixtureVisionAdapter,
)
from shopsteward.adapters.vision.interface import (
    CopyCheckResult,
    CopyCheckVerdict,
    VisionResult,
    VisionVerdict,
)
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


def _cap(adapter, *results, vision=None) -> ListingCatalogExpand:
    """Live-gates open (precondition_ok) with a queued fake copy checker."""
    return ListingCatalogExpand(
        adapter,
        live_copy=True,
        checker=FakeCopyChecker(list(results)),
        live_check=True,
        vision=vision or FixtureVisionAdapter(),
        live_vision=True,
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
        adapter,
        live_copy=True,
        checker=FixtureCopyChecker(),
        live_check=False,
        vision=FixtureVisionAdapter(),
        live_vision=False,
    )
    assert closed.precondition_ok is False
    cfg, _ = _auto_run(conn, tmp_path, closed)
    (action,) = closed.propose(conn, USER_ID, cfg)
    assert govern(conn, USER_ID, action, closed, cfg, TODAY).reason == RefusalReason.PRECONDITION
    assert adapter.listings == {}

    # Even if execute() is reached directly, the fixture checker never approves.
    forced = ListingCatalogExpand(
        adapter,
        live_copy=True,
        checker=FixtureCopyChecker(),
        live_check=True,
        vision=FixtureVisionAdapter(),
        live_vision=True,
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
        FakeEtsyWriteAdapter(),
        live_copy=False,
        checker=FakeCopyChecker([]),
        live_check=True,
        vision=FixtureVisionAdapter(),
        live_vision=True,
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

    run(conn, USER_ID, cfg, [cap], today=TODAY)
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


def _verdict_result() -> VisionResult:
    return VisionResult(
        verdict=VisionVerdict(
            commercial_score=70,
            subject="osprey",
            strongest_room_style="coastal",
            one_risk="glare",
            rationale="well composed",
            scene="An osprey perched on a bare branch against a clear sky.",
        )
    )


def _one_candidate_cfg(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    photo = _candidate_image(folder, "one.jpg")
    cfg = _apply_ops_cfg_max_new_per_week(conn, tmp_path, source_folder=folder, max_new_per_week=5)
    return cfg, hashlib.sha256(photo.read_bytes()).hexdigest()


def test_closed_vision_gate_refuses_at_governor_and_ingests_nothing(conn, tmp_path, offline_copy):
    cfg, _ = _one_candidate_cfg(conn, tmp_path)
    vision = FakeVisionAdapter([])
    cap = ListingCatalogExpand(
        FakeEtsyWriteAdapter(),
        live_copy=True,
        checker=FakeCopyChecker([]),
        live_check=True,
        vision=vision,
        live_vision=False,
    )
    assert cap.precondition_ok is False
    register(cap)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert (report.executed, report.refused) == (0, 1)
    assert read_all(conn, "landing.file_") == []  # nothing ingested
    assert vision.calls == []


def test_expand_scores_the_file_then_builds_a_titled_draft(conn, tmp_path, offline_copy):
    cfg, file_id = _one_candidate_cfg(conn, tmp_path)  # NOT pre-scored
    vision = FakeVisionAdapter([_verdict_result()])
    cap = _cap(FakeEtsyWriteAdapter(), _ok(), vision=vision)
    register(cap)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert (report.executed, report.refused, report.failed) == (1, 0, 0)
    assert len(vision.calls) == 1
    (scored,) = read_all(conn, "photo.scored")
    assert scored.payload["photo_id"] == f"file-{file_id[:12]}"
    row = conn.execute(
        "SELECT title FROM proj_listing_drafts WHERE landing_file_id=?", (file_id,)
    ).fetchone()
    assert row["title"] is not None
    assert _candidates(conn, USER_ID, cfg) == {}  # a completed expansion is not a candidate


def test_file_ingested_by_a_failed_attempt_is_retried(conn, tmp_path, offline_copy):
    cfg, file_id = _one_candidate_cfg(conn, tmp_path)
    adapter = FakeEtsyWriteAdapter()
    # attempt 1: vision down -> file ingested, no title, retryable
    cap1 = _cap(adapter, _ok(), vision=FakeVisionAdapter([RuntimeError("vision down")]))
    register(cap1)
    report = run(conn, USER_ID, cfg, [cap1], today=TODAY, live_autonomy=True)
    assert report.executed == 0
    assert read_all(conn, "landing.file_")  # the failed attempt did ingest it
    assert file_id in _candidates(conn, USER_ID, cfg)  # ...and it is still a candidate

    # attempt 2 (the retryable refusal stays approvable): vision is back -> expands
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    REGISTRY.clear()
    cap2 = _cap(adapter, _ok(), vision=FakeVisionAdapter([_verdict_result()]))
    register(cap2)
    report = approve_action(
        conn, USER_ID, action_id, [cap2], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert report.executed == 1
    assert len(adapter.listings) == 1
    assert len(read_all(conn, "asset.archived")) == 1  # archive no-ops on retry
    assert _candidates(conn, USER_ID, cfg) == {}


def test_file_with_copy_refused_max_times_is_not_a_candidate(conn, tmp_path, offline_copy):
    from shopsteward.pipeline.listings.copy import MAX_COPY_REFUSALS

    cfg, file_id = _one_candidate_cfg(conn, tmp_path)
    cap = _cap(
        FakeEtsyWriteAdapter(), _ok(), vision=FakeVisionAdapter([RuntimeError("vision down")])
    )
    register(cap)
    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    draft = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE landing_file_id=?", (file_id,)
    ).fetchone()
    assert draft is not None and file_id in _candidates(conn, USER_ID, cfg)

    for _ in range(MAX_COPY_REFUSALS):
        append(
            conn,
            Event(
                user_id=USER_ID,
                type="copy.refused",
                payload={"draft_id": draft["draft_id"], "reason": "undelivered size"},
            ),
        )
    assert _candidates(conn, USER_ID, cfg) == {}


def test_adopted_file_with_etsy_id_and_no_title_is_not_a_candidate(conn, tmp_path):
    from shopsteward.pipeline import tuning
    from shopsteward.pipeline.config import TUNING_PROFILE_PATH
    from shopsteward.pipeline.listings import adopt

    cfg, file_id = _one_candidate_cfg(conn, tmp_path)
    tuning.seed(conn, USER_ID, TUNING_PROFILE_PATH)
    assert adopt.ingest_one_file(conn, USER_ID, tmp_path / "candidates" / "one.jpg") == file_id
    assert file_id in _candidates(conn, USER_ID, cfg)  # known, unfinished -> still a candidate
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listing.source_adopted",
            payload={
                "draft_id": "adopted-1",
                "landing_file_id": file_id,
                "photo_id": f"file-{file_id[:12]}",
                "etsy_listing_id": 777,
            },
        ),
    )
    assert _candidates(conn, USER_ID, cfg) == {}


def _fail(conn, action, kind: str) -> None:
    """One failed attempt for `action`: action.failed or the revalidation refusal."""
    if kind == "failed":
        append(conn, Event(user_id=USER_ID, type="action.failed", payload={"action_id": action}))
    else:
        append(
            conn,
            Event(
                user_id=USER_ID,
                type="action.refused",
                payload={"action_id": action, "reason": "execute_revalidation_error"},
            ),
        )


def _propose_for(conn, cfg, file_id: str, action_id: str, *, deduped: bool = False) -> None:
    action = _candidates(conn, USER_ID, cfg)[file_id]
    if deduped:
        payload = {"action_id": action_id, "capability": action.capability, "target_id": file_id}
        append(conn, Event(user_id=USER_ID, type="action.proposal_deduped", payload=payload))
    else:
        payload = action.model_dump() | {"action_id": action_id}
        append(conn, Event(user_id=USER_ID, type="action.proposed", payload=payload))


def test_three_failed_attempts_stop_a_file_being_a_candidate(conn, tmp_path):
    cfg, file_id = _one_candidate_cfg(conn, tmp_path)
    _propose_for(conn, cfg, file_id, "a1")
    _propose_for(conn, cfg, file_id, "a2", deduped=True)
    _fail(conn, "a1", "failed")
    _fail(conn, "a1", "refused")
    assert file_id in _candidates(conn, USER_ID, cfg)  # 2 attempts: still retried
    _fail(conn, "a2", "refused")  # resolved through action.proposal_deduped
    assert _candidates(conn, USER_ID, cfg) == {}


def test_fresh_files_sort_ahead_of_retried_ones(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    a = _candidate_image(folder, "a_retried.jpg")
    b = _candidate_image(folder, "b_fresh.jpg", ellipse=True)
    cfg = _apply_ops_cfg_max_new_per_week(conn, tmp_path, source_folder=folder, max_new_per_week=5)
    id_a = hashlib.sha256(a.read_bytes()).hexdigest()
    id_b = hashlib.sha256(b.read_bytes()).hexdigest()
    assert list(_candidates(conn, USER_ID, cfg)) == [id_a, id_b]  # path order

    _propose_for(conn, cfg, id_a, "a1")
    _fail(conn, "a1", "failed")
    assert list(_candidates(conn, USER_ID, cfg)) == [id_b, id_a]


def test_a_candidate_matching_a_catalog_raw_builds_under_the_catalog_photo_id(
    conn, tmp_path, offline_copy
):
    """Nearly every candidate is an export of a catalog RAW, so its landing row
    carries that photo_id. expand_one used the synthetic file-<id> instead, and
    mockups (keyed on the landing row's photo_id) rendered nothing, so no draft
    was ever built (2026-11-24 weekly runs, 10 photos)."""
    from shopsteward.editing.ingest import ingest_folder
    from shopsteward.editing.projections import rebuild_editing

    shoot = tmp_path / "shoot"
    shoot.mkdir()
    (shoot / "one.CR3").write_bytes(b"raw-one")
    Image.new("RGB", (20, 20)).save(shoot / "one.jpg", "JPEG")
    (photo_id,) = ingest_folder(conn, user_id=USER_ID, path=shoot, mode="hero").photo_ids
    rebuild_editing(conn)

    cfg, file_id = _one_candidate_cfg(conn, tmp_path)  # candidates/one.jpg
    # An unlisted sibling export of the SAME RAW, already a landing file: it
    # shares the catalog photo_id but must never get mockups or a draft.
    from shopsteward.pipeline import tuning
    from shopsteward.pipeline.config import TUNING_PROFILE_PATH
    from shopsteward.pipeline.listings import adopt

    tuning.seed(conn, USER_ID, TUNING_PROFILE_PATH)
    sibling = _candidate_image(tmp_path / "older_exports", "one-alt.jpg")
    assert adopt.ingest_one_file(conn, USER_ID, sibling) is not None
    vision = FakeVisionAdapter([_verdict_result()])
    cap = _cap(FakeEtsyWriteAdapter(), _ok(), vision=vision)
    register(cap)

    report = run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)
    assert (report.executed, report.refused, report.failed) == (1, 0, 0)
    (scored,) = read_all(conn, "photo.scored")
    assert scored.payload["photo_id"] == photo_id
    row = conn.execute(
        "SELECT title, photo_id FROM proj_listing_drafts WHERE landing_file_id=?", (file_id,)
    ).fetchone()
    assert row["title"] is not None and row["photo_id"] == photo_id
    drafts = conn.execute(
        "SELECT landing_file_id FROM proj_listing_drafts WHERE photo_id=?", (photo_id,)
    ).fetchall()
    assert [d["landing_file_id"] for d in drafts] == [file_id]  # no sibling draft


def test_dust_verdict_fails_the_gate_and_leaves_a_draft(conn, tmp_path, offline_copy):
    adapter = FakeEtsyWriteAdapter()
    dusty = CopyCheckResult(verdict=CopyCheckVerdict(ok=True, dust="flat sky, upper left"))
    cap = _cap(adapter, dusty)
    cfg, _ = _auto_run(conn, tmp_path, cap)

    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)

    (listing_id,) = adapter.listings
    assert adapter.listings[listing_id]["state"] == "draft"
    assert "publish_listing" not in [c for c, _ in adapter.calls]
    (rb,) = read_all(conn, "listingdraft.readback")
    assert rb.payload["ok"] is False
    assert rb.payload["problems"] == ["sensor dust: flat sky, upper left"]
    (failure,) = _brief(conn, cfg).readback_failed
    assert failure.problems == ["sensor dust: flat sky, upper left"]


def test_no_dust_verdict_publishes(conn, tmp_path, offline_copy):
    adapter = FakeEtsyWriteAdapter()
    clean = CopyCheckResult(verdict=CopyCheckVerdict(ok=True, dust=""))
    cap = _cap(adapter, clean)
    cfg, _ = _auto_run(conn, tmp_path, cap)

    run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)

    (listing_id,) = adapter.listings
    assert adapter.listings[listing_id]["state"] == "active"


def test_copy_check_prompt_excludes_dust_false_positives():
    from shopsteward.pipeline.config import COPY_CHECK_PROMPT_PATH

    text = COPY_CHECK_PROMPT_PATH.read_text(encoding="utf-8").lower()
    for word in ("birds", "stars", "distant animals", "snowflakes", "intentional", "dust"):
        assert word in text
