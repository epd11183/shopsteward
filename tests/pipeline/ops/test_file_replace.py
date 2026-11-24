"""`listing.file_replace`: swap the rank-1 digital file. Fakes only."""

import pytest
from PIL import Image

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.digital_files import encode_file, plan_files
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.file_replace import ListingFileReplace
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import LIVE_GATED_CAPABILITIES, approve_action, run
from tests.pipeline.listings.test_slice4_backfill import LID, Flaky, _refresh, _run, _world
from tests.pipeline.ops.helpers import USER_ID


class Hook(Flaky):
    fail_delete: set[int] = set()
    fail_upload_after_delete = False

    def delete_listing_file(self, listing_id, listing_file_id):
        if listing_file_id in self.fail_delete:
            raise EtsyWriteError(500, "boom")
        super().delete_listing_file(listing_id, listing_file_id)
        if self.fail_upload_after_delete:
            self.fail_on = self.n + 1


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


def _setup(conn, tmp_path, *, backfill=False, legacy=True):
    adapter = _world(conn, tmp_path, legacy=legacy, adapter=Hook())
    if backfill:
        assert _run(conn, adapter).results[0].status == "uploaded"
    adapter.calls.clear()
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    _refresh(conn)
    healed = tmp_path / "healed.jpg"
    Image.new("RGB", (600, 400), (200, 100, 50)).save(healed, "JPEG")
    cap = ListingFileReplace(adapter)
    register(cap)
    return adapter, ops_config.get_ops_config(conn, USER_ID), cap, healed


def _go(conn, cfg, cap, healed):
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True)
    return approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, live_autonomy=True)


def _names(adapter):
    return {f.rank: (f.listing_file_id, f.name) for f in adapter.list_listing_files(LID)}


def _kinds(adapter):
    return [k for k, _ in adapter.calls if k.endswith("listing_file")]


def test_tier_is_propose_with_no_undo_and_gated():
    cap = ListingFileReplace(FakeEtsyWriteAdapter())
    assert cap.max_tier == Tier.PROPOSE and cap.undo is None
    assert cap.key in LIVE_GATED_CAPABILITIES
    assert cap.key not in ops_config.load_ops_config().autonomy.tier_floor
    register(cap)  # invariant 1 allows undo=None at PROPOSE


def test_under_cap_uploads_first_then_deletes_and_keeps_name(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)
    old_id = _names(adapter)[1][0]
    adapter.calls.clear()
    assert _go(conn, cfg, cap, healed).executed == 1
    assert _kinds(adapter) == ["upload_listing_file", "delete_listing_file"]
    [(new_id, name)] = _names(adapter).values()
    assert new_id != old_id and name == "photo-1.jpg"
    assert [e.payload["new_file_id"] for e in read_all(conn, "listingfile.replaced")] == [new_id]


def test_at_cap_deletes_first_and_leaves_crops_alone(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path, backfill=True)
    before = _names(adapter)
    assert len(before) == 5
    adapter.calls.clear()
    assert _go(conn, cfg, cap, healed).executed == 1
    assert _kinds(adapter)[-2:] == ["delete_listing_file", "upload_listing_file"]
    after = _names(adapter)
    assert len(after) == 5 and after[1][0] != before[1][0]
    assert {r: v for r, v in after.items() if r > 1} == {r: v for r, v in before.items() if r > 1}


def test_at_cap_upload_failure_leaves_no_rank1_and_rerun_resumes(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path, backfill=True)
    adapter.fail_upload_after_delete = True
    assert _go(conn, cfg, cap, healed).refused == 1
    assert 1 not in _names(adapter) and len(_names(adapter)) == 4
    adapter.fail_upload_after_delete = False
    adapter.fail_on = None
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    assert action.params["old_file_id"] == 0
    run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True)
    approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, live_autonomy=True)
    assert _names(adapter)[1][1] == "photo-1.jpg" and len(_names(adapter)) == 5


def test_both_live_records_orphan_and_refuses_the_listing(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)
    old_id = _names(adapter)[1][0]
    adapter.fail_delete = {old_id, old_id + 1}  # old delete fails; new (next id) too
    assert _go(conn, cfg, cap, healed).executed == 0
    assert len(adapter.list_listing_files(LID)) == 2
    assert len(read_all(conn, "listingfile.replace_orphaned")) == 1
    with pytest.raises(ValueError, match="both live"):
        cap.build_action(conn, USER_ID, cfg, LID, healed)


def test_delete_failure_with_clean_cleanup_leaves_one_file(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)
    old_id = _names(adapter)[1][0]
    adapter.fail_delete = {old_id}
    assert _go(conn, cfg, cap, healed).refused == 1
    assert [f.listing_file_id for f in adapter.list_listing_files(LID)] == [old_id]


def test_refuses_unknown_non_digital_and_missing_rank1(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)
    with pytest.raises(ValueError, match="not a known digital"):
        cap.build_action(conn, USER_ID, cfg, 424242, healed)
    conn.execute("UPDATE proj_listing_drafts SET provider='printful'")
    with pytest.raises(ValueError, match="not a known digital"):
        cap.build_action(conn, USER_ID, cfg, LID, healed)
    conn.execute("UPDATE proj_listing_drafts SET provider='etsy_digital'")
    adapter.listings[LID]["files"][0]["rank"] = 2  # no rank-1 file
    with pytest.raises(ValueError, match="exactly one rank-1"):
        cap.build_action(conn, USER_ID, cfg, LID, healed)
    adapter.listings[LID]["files"][0]["rank"] = 1
    with pytest.raises(ValueError):
        cap.build_action(conn, USER_ID, cfg, LID, tmp_path / "missing.jpg")


def test_file_edited_after_proposal_is_stale_not_executed(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True)
    Image.new("RGB", (600, 400), (1, 2, 3)).save(healed, "JPEG")
    approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, live_autonomy=True)
    assert not read_all(conn, "listingfile.replaced")
    assert len(adapter.list_listing_files(LID)) == 1


def test_live_gate_blocks_execution(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True)
    adapter.calls.clear()
    r = approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, live_autonomy=False)
    assert r.live_gate_blocked and not adapter.calls


def test_encoding_is_the_pipeline_rank1_encoding(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)
    dcfg = listing_config.get_config(conn, USER_ID).digital_files
    full = plan_files((600, 400), (0.5, 0.5), dcfg).files[0]
    data, sha = encode_file(healed, full, dcfg)
    assert full.key == "full" and full.rank == 1
    assert _go(conn, cfg, cap, healed).executed == 1
    [(_, kw)] = [c for c in adapter.calls if c[0] == "upload_listing_file"]
    assert kw["size"] == len(data) and kw["rank"] == 1 and kw["size"] <= dcfg.max_bytes
    done = [e.payload for e in read_all(conn, "action.executed")][-1]
    assert done["after"]["sha256"] == sha
