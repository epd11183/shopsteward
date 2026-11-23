"""`listing.panel_replace` (slice 5b): swap the stale "what you get" image.
Fakes only; synthetic photo."""

import json

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.digital_files import plan_sizes
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.panel_replace import ListingPanelReplace
from shopsteward.pipeline.ops.capabilities.size_claims_fix import _delivered_plan
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from tests.pipeline.listings.test_slice4_backfill import LID, _refresh, _run, _world
from tests.pipeline.ops.helpers import USER_ID

RANK = 7
OLD_SIZES = ["4x6", "8x12", "16x20"]  # the old global panel: claims what files don't deliver


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


def _ev(conn, type_, payload):
    append(conn, Event(user_id=USER_ID, type=type_, payload=payload))


def _mockup(path, sizes):
    return {
        "photo_id": "photo-1",
        "landing_file_id": "f" * 64,
        "set_key": "set-1",
        "intent": "digital_whatyougot",
        "template_id": None,
        "path": path,
        "params": {"sizes": sizes},
    }


def _setup(conn, tmp_path, *, panels=1):
    """Backfilled listing whose pipeline-pushed draft carries `panels` panel
    entries (ranks 7, 8, ...), each with a recorded old render."""
    adapter = _world(conn, tmp_path)
    assert _run(conn, adapter).results[0].status == "uploaded"
    draft = f"adopted-{LID}"
    images = [
        {"path": str(tmp_path / f"panel{i}.jpg"), "intent": "digital_whatyougot", "rank": RANK + i}
        for i in range(panels)
    ]
    _ev(conn, "listingdraft.pushed_to_etsy", {"draft_id": draft, "etsy_listing_id": LID})
    _ev(conn, "listingdraft.images_selected", {"draft_id": draft, "images": images})
    for img in images:
        ref = adapter.upload_listing_image(LID, b"old-panel", rank=img["rank"])
        attached = [{"etsy_image_id": ref.listing_image_id, "rank": img["rank"]}]
        _ev(
            conn,
            "listingdraft.images_attached",
            {"draft_id": draft, "etsy_listing_id": LID, "images": attached},
        )
        _ev(conn, "mockup.generated", _mockup(img["path"], OLD_SIZES))
    _ev(
        conn,
        "etsy.listing.observed",
        {
            "listing_id": LID,
            "title": "Misty Ridge",
            "state": "active",
            "quantity": 999,
            "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"},
            "tags": [],
            "description": "x",
        },
    )
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    _refresh(conn)
    adapter.calls.clear()
    return adapter, ops_config.get_ops_config(conn, USER_ID)


def _panel(conn):
    row = conn.execute(
        "SELECT images_json FROM proj_listing_drafts WHERE draft_id=?", (f"adopted-{LID}",)
    ).fetchone()
    return json.loads(row["images_json"])[0]


def _gallery(adapter, lid=LID):
    return {i["listing_image_id"]: i["rank"] for i in adapter.listings[lid]["images"]}


def test_round_trip_restores_original_image_at_original_rank(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    old_id = _panel(conn)["etsy_image_id"]
    cap = ListingPanelReplace(adapter)
    assert cap.max_tier == Tier.AUTO
    register(cap)
    [a] = cap.propose(conn, USER_ID, cfg)
    assert a.params["old_image_id"] == old_id and a.params["rank"] == RANK
    assert a.params["old_sizes"] == OLD_SIZES and a.undo_available

    run(conn, USER_ID, cfg, [cap])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, live_autonomy=True)

    [(new_id, new_rank)] = _gallery(adapter).items()
    assert new_id != old_id and new_rank == RANK
    _refresh(conn)
    panel = _panel(conn)
    assert panel["etsy_image_id"] == new_id and panel["panel_sizes"] == a.params["new_sizes"]
    assert cap.propose(conn, USER_ID, cfg) == []  # current now -> not re-proposed
    done = read_all(conn, "action.executed")[0].payload
    assert done["before"]["image_id"] == old_id and done["after"]["image_id"] == new_id

    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
    assert _gallery(adapter) == {old_id: RANK}
    _refresh(conn)
    assert _panel(conn)["etsy_image_id"] == old_id


def test_current_panel_yields_no_proposal(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    plan = _delivered_plan(conn, USER_ID, LID, adapter)
    assert plan is not None and plan_sizes(plan)
    _ev(conn, "mockup.generated", _mockup(str(tmp_path / "panel0.jpg"), plan_sizes(plan)))
    _refresh(conn)
    assert ListingPanelReplace(adapter).propose(conn, USER_ID, cfg) == []


def test_unidentifiable_panel_yields_no_proposal(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path, panels=2)  # two panel entries: which one?
    assert ListingPanelReplace(adapter).propose(conn, USER_ID, cfg) == []


def test_adopted_draft_without_images_yields_no_proposal(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    assert _run(conn, adapter).results[0].status == "uploaded"
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert ListingPanelReplace(adapter).propose(conn, USER_ID, cfg) == []


def test_fake_reattach_of_deleted_image_and_unknown_id():
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(5)
    ref = fake.upload_listing_image(5, b"x", rank=3)
    fake.delete_listing_image(5, ref.listing_image_id)
    assert fake.listings[5]["images"] == []
    back = fake.attach_listing_image(5, ref.listing_image_id, rank=3)
    assert back.listing_image_id == ref.listing_image_id and back.rank == 3
    assert _gallery(fake, 5) == {ref.listing_image_id: 3}
    with pytest.raises(EtsyWriteError):  # a live image is not "deleted"
        fake.attach_listing_image(5, ref.listing_image_id, rank=3)
    with pytest.raises(EtsyWriteError):
        fake.attach_listing_image(5, 999, rank=1)
    with pytest.raises(EtsyWriteError):
        fake.delete_listing_image(5, 999)
