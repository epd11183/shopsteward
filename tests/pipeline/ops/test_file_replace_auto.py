"""`listing.file_replace` at AUTO: only behind a sha-verified undo copy and a
same-photo (pHash) check; everything else stays proposed. Fakes only."""

import json
from datetime import date, timedelta

import pytest
from PIL import Image, ImageDraw

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.backfill import check_preconditions
from shopsteward.pipeline.listings.digital_files import encode_file, photo_dims, plan_files
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.file_replace import ListingFileReplace
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.listings.test_slice4_backfill import LID, _refresh, _world
from tests.pipeline.ops.helpers import USER_ID
from tests.pipeline.ops.test_file_replace import Hook, _setup


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


def _textured(path, *, dust=False, stripes=False):
    img = Image.new("RGB", (600, 400), (20, 40, 80))
    d = ImageDraw.Draw(img)
    if stripes:
        for x in range(0, 600, 40):
            d.line((x, 0, x, 400), fill=(255, 255, 255), width=6)
    else:
        d.ellipse((150, 100, 450, 300), fill=(230, 160, 40))
    if dust:
        d.ellipse((10, 10, 14, 14), fill=(0, 0, 0))
    img.save(path, "JPEG", quality=95)


def _verified(conn, tmp_path, *, extra_files=0):
    """An adopted listing whose rank-1 file is the pipeline encoding of its master,
    with its sha256 recorded (listingdraft.file_attached): an undo source exists."""
    adapter = _world(conn, tmp_path, legacy=False, adapter=Hook())
    master = tmp_path / "photo-1.jpg"
    _textured(master)
    edited = asset_store_config.load_asset_store_config().model_dump(by_alias=True)
    edited["root"] = str(tmp_path / "archive")
    (tmp_path / "asset_store.json").write_text(json.dumps(edited))
    asset_store_config.apply(conn, USER_ID, tmp_path / "asset_store.json")
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    _refresh(conn)
    dcfg = check_preconditions(listing_config.get_config(conn, USER_ID), live=True)
    full = plan_files(photo_dims(master), (0.5, 0.5), dcfg).files[0]
    old, sha = encode_file(master, full, dcfg)
    ref = adapter.upload_listing_file(LID, old, name="photo-1.jpg", rank=1)
    for rank in range(2, 2 + extra_files):
        adapter.upload_listing_file(LID, b"crop", name=f"crop-{rank}.jpg", rank=rank)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.file_attached",
            payload={
                "draft_id": f"adopted-{LID}",
                "etsy_listing_id": LID,
                "etsy_file_id": ref.listing_file_id,
                "source": "digital_files",
                "sha256": sha,
                "key": "full",
                "rank": 1,
                "name": "photo-1.jpg",
                "sizes": [],
            },
        ),
    )
    _refresh(conn)
    adapter.calls.clear()
    adapter.uploaded.clear()
    cap = ListingFileReplace(adapter)
    register(cap)
    healed = tmp_path / "healed.jpg"
    _textured(healed, dust=True)
    return adapter, ops_config.get_ops_config(conn, USER_ID), cap, healed, old


def _event_types(conn, action_id):
    return [e.type for e in read_all(conn) if e.payload.get("action_id") == action_id]


@pytest.mark.parametrize("extra_files", [0, 4])  # under the cap, then at Etsy's 5-file cap
def test_verified_swap_auto_executes_and_undo_restores_the_old_bytes(conn, tmp_path, extra_files):
    adapter, cfg, cap, healed, old = _verified(conn, tmp_path, extra_files=extra_files)
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    assert action.params["auto_hold"] == "" and action.undo_available

    report = run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True)

    assert report.executed == 1  # AUTO: no approve step
    (rep,) = [e.payload for e in read_all(conn, "listingfile.replaced")]
    assert rep["old_sha256"] and rep["old_sha256"] != rep["new_sha256"]
    stash = list((tmp_path / "archive" / "replaced_files").glob("*.jpg"))
    assert [p.read_bytes() for p in stash] == [old]  # kept BEFORE the delete
    assert adapter.uploaded[0] != old
    assert len(adapter.list_listing_files(LID)) == 1 + extra_files

    adapter.uploaded.clear()
    undo_action(conn, USER_ID, action.action_id, [cap], live_autonomy=True)

    assert adapter.uploaded == [old]  # the old bytes, byte for byte
    files = {f.rank: f for f in adapter.list_listing_files(LID)}
    assert files[1].name == "photo-1.jpg" and files[1].listing_file_id != rep["new_file_id"]
    assert len(files) == 1 + extra_files
    assert "action.undone" in _event_types(conn, action.action_id)
    assert len(read_all(conn, "listingfile.replace_undone")) == 1


def test_phash_mismatch_stays_proposed_with_the_reason_and_is_not_redriven(conn, tmp_path):
    adapter, cfg, cap, _, _ = _verified(conn, tmp_path)
    other = tmp_path / "other.jpg"
    _textured(other, stripes=True)
    action = cap.build_action(conn, USER_ID, cfg, LID, other)
    assert "not the same photo" in action.params["auto_hold"]
    assert "HELD for operator" in action.reason and not action.undo_available

    assert run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True).executed == 0
    # the next weekly run re-drives pending proposals at AUTO: still held
    assert run(conn, USER_ID, cfg, [cap], live_autonomy=True).executed == 0
    assert not read_all(conn, "listingfile.replaced")
    assert adapter.uploaded == []


def test_no_undo_source_stays_proposed_until_the_operator_approves(conn, tmp_path):
    adapter, cfg, cap, healed = _setup(conn, tmp_path)  # adopted: legacy file, no recorded sha
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    assert "no recorded sha256" in action.params["auto_hold"]

    assert run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True).executed == 0
    assert run(conn, USER_ID, cfg, [cap], live_autonomy=True).executed == 0
    assert not read_all(conn, "listingfile.replaced")

    # the operator may still approve it; there is then no undo copy
    approve_action(conn, USER_ID, action.action_id, [cap], cfg=cfg, live_autonomy=True)
    (rep,) = [e.payload for e in read_all(conn, "listingfile.replaced")]
    assert rep["old_sha256"] is None
    with pytest.raises(ValueError, match="no undo copy"):
        cap.undo(conn, USER_ID, action)


def test_unreproducible_source_stays_proposed(conn, tmp_path):
    adapter, cfg, cap, healed, _ = _verified(conn, tmp_path)
    _textured(tmp_path / "photo-1.jpg", stripes=True)  # master no longer yields the old bytes
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    assert "not reproducible" in action.params["auto_hold"]
    assert run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True).executed == 0


def test_undo_at_the_cap_leaves_no_resume_marker_and_stash_is_recorded(conn, tmp_path):
    adapter, cfg, cap, healed, old = _verified(conn, tmp_path, extra_files=4)
    action = cap.build_action(conn, USER_ID, cfg, LID, healed)
    assert run(conn, USER_ID, cfg, [cap], proposals=[action], live_autonomy=True).executed == 1
    (marker,) = read_all(conn, "listingfile.replace_old_deleted")
    assert marker.payload["action_id"] == action.action_id and marker.payload["old_sha256"]

    undo_action(conn, USER_ID, action.action_id, [cap], live_autonomy=True)

    healed2 = tmp_path / "healed2.jpg"  # a new file => a new action_id
    _textured(healed2, dust=True, stripes=False)
    ImageDraw.Draw(img := Image.open(healed2)).ellipse((30, 30, 36, 36), fill=(0, 0, 0))
    img.save(healed2, "JPEG", quality=95)
    again = cap.build_action(conn, USER_ID, cfg, LID, healed2)
    assert again.params["old_file_id"] != 0  # a normal swap, not a resume
    adapter.calls.clear()
    next_week = date.today() + timedelta(days=8)  # the first swap used this week's portfolio cap
    r = run(conn, USER_ID, cfg, [cap], proposals=[again], live_autonomy=True, today=next_week)
    assert r.executed == 1
    kinds = [k for k, _ in adapter.calls if k.endswith("listing_file")]
    assert kinds[-2:] == ["delete_listing_file", "upload_listing_file"]
    assert len(adapter.list_listing_files(LID)) == 5
