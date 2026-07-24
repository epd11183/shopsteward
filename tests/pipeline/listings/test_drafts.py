import hashlib

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.drafts import build_drafts

from .helpers import USER_ID, seed_landing_file_with_mockup_set


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_one(conn, tmp_path, *, file_id="f" * 64, photo_id="photo-1"):
    path = tmp_path / f"{photo_id}.jpg"
    Image.new("RGB", (100, 100), (5, 6, 7)).save(path, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id=file_id,
        photo_id=photo_id,
        path=str(path),
        set_key=f"set-{file_id}",
        intents=["framed_poster", "single", "digital_whatyougot"],
    )
    return path


def test_empty_landing_is_a_no_op(conn):
    result = build_drafts(conn, USER_ID)
    assert result.drafts_built == 0
    assert result.skipped_idempotent == 0


def test_landing_file_without_mockup_set_is_skipped(conn):
    from shopsteward.core.events import Event, append
    from shopsteward.pipeline.projections import rebuild_pipeline

    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": "a" * 64,
                "path": "/landing/no-mockups.jpg",
                "base_name": None,
                "format": "JPEG",
                "width": 100,
                "height": 100,
                "color_space": "sRGB",
                "photo_id": "photo-no-mockups",
            },
        ),
    )
    rebuild_pipeline(conn)

    result = build_drafts(conn, USER_ID)
    assert result.drafts_built == 0


def test_build_emits_created_and_images_selected(conn, tmp_path):
    _seed_one(conn, tmp_path)

    result = build_drafts(conn, USER_ID)
    assert result.drafts_built == 1

    created = [e for e in read_all(conn, "listingdraft.created") if e.user_id == USER_ID]
    images_selected = [
        e for e in read_all(conn, "listingdraft.images_selected") if e.user_id == USER_ID
    ]
    assert len(created) == 1
    assert len(images_selected) == 1
    assert created[0].payload["draft_id"] == images_selected[0].payload["draft_id"]

    payload = created[0].payload
    assert payload["provider"] == "etsy_digital"
    assert payload["format"] == "digital_download"
    assert payload["sku_source"] == "etsy"
    assert payload["listing_type"] == "download"
    assert payload["photo_id"] == "photo-1"
    # downstream contract: projections + later slices read these from the payload
    assert payload["set_key"], "set_key must be carried in listingdraft.created"
    assert len(payload["config_hash"]) == 64  # sha256 hex of the listing config
    assert payload["landing_file_id"]


def test_image_order_hero_single_first_and_whatyougot_present(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)

    images_selected = [
        e for e in read_all(conn, "listingdraft.images_selected") if e.user_id == USER_ID
    ][0]
    images = images_selected.payload["images"]
    assert images[0]["intent"] == "single"
    assert images[0]["rank"] == 1
    assert any(img["intent"] == "digital_whatyougot" for img in images)


def test_sellable_file_is_landing_original_not_a_mockup(conn, tmp_path):
    path = _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)

    images_selected = [
        e for e in read_all(conn, "listingdraft.images_selected") if e.user_id == USER_ID
    ][0]
    sellable = images_selected.payload["sellable_file"]
    assert sellable["source"] == "landing_original"
    assert sellable["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    mockup_paths = {img["path"] for img in images_selected.payload["images"]}
    assert str(path) not in mockup_paths


def test_draft_id_is_stable_across_runs(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)
    created = [e for e in read_all(conn, "listingdraft.created") if e.user_id == USER_ID]
    first_id = created[0].payload["draft_id"]

    cfg = listing_config.get_config(conn, USER_ID)
    cfg_hash = listing_config.config_hash(cfg)
    expected = hashlib.sha256(f"{'f' * 64}|{cfg_hash}|set-{'f' * 64}".encode()).hexdigest()
    assert first_id == expected


def test_build_is_idempotent_second_run_emits_nothing_new(conn, tmp_path):
    _seed_one(conn, tmp_path)
    first = build_drafts(conn, USER_ID)
    assert first.drafts_built == 1

    second = build_drafts(conn, USER_ID)
    assert second.drafts_built == 0
    assert second.skipped_idempotent == 1

    created = [e for e in read_all(conn, "listingdraft.created") if e.user_id == USER_ID]
    assert len(created) == 1


def test_build_force_rebuilds_images(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)

    forced = build_drafts(conn, USER_ID, force=True)
    assert forced.drafts_built == 1

    created = [e for e in read_all(conn, "listingdraft.created") if e.user_id == USER_ID]
    assert len(created) == 2  # one per run, mirrors mockups --force precedent


def test_build_photo_id_filter(conn, tmp_path):
    _seed_one(conn, tmp_path, file_id="a" * 64, photo_id="photo-a")
    _seed_one(conn, tmp_path, file_id="b" * 64, photo_id="photo-b")

    result = build_drafts(conn, USER_ID, photo_id="photo-a")
    assert result.drafts_built == 1

    created = [e for e in read_all(conn, "listingdraft.created") if e.user_id == USER_ID]
    assert len(created) == 1
    assert created[0].payload["photo_id"] == "photo-a"
