import hashlib

import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings

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


def test_build_emits_copy_generated_and_priced(conn, tmp_path):
    _seed_one(conn, tmp_path)
    result = build_drafts(conn, USER_ID)
    assert result.copy_calls == 1

    copy_events = [e for e in read_all(conn, "listingdraft.copy_generated") if e.user_id == USER_ID]
    priced_events = [e for e in read_all(conn, "listingdraft.priced") if e.user_id == USER_ID]
    assert len(copy_events) == 1
    assert len(priced_events) == 1
    assert priced_events[0].payload["price"] == 12.00
    assert priced_events[0].payload["margin_floor"] == 6.00
    assert priced_events[0].payload["currency"] == "USD"
    assert priced_events[0].payload["auto"] is True


def test_fully_built_draft_is_skipped_without_force(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)

    second = build_drafts(conn, USER_ID)
    assert second.skipped_idempotent == 1
    assert second.drafts_built == 0
    assert len(read_all(conn, "listingdraft.copy_generated")) == 1
    assert len(read_all(conn, "listingdraft.priced")) == 1


def _draft_id_for(conn, landing_file_id: str, set_key: str) -> str:
    cfg = listing_config.get_config(conn, USER_ID)
    cfg_hash = listing_config.config_hash(cfg)
    return hashlib.sha256(f"{landing_file_id}|{cfg_hash}|{set_key}".encode()).hexdigest()


def _seed_slice1_only_draft(conn, *, file_id: str, set_key: str, photo_id: str) -> str:
    """Appends only listingdraft.created + .images_selected (no copy/priced),
    the way a draft built before slice 2 shipped would look -- exercises
    fill-forward without ever deleting an event row."""
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    draft_id = _draft_id_for(conn, file_id, set_key)
    cfg_hash = listing_config.config_hash(listing_config.get_config(conn, USER_ID))
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.created",
            payload={
                "draft_id": draft_id,
                "landing_file_id": file_id,
                "photo_id": photo_id,
                "set_key": set_key,
                "provider": "etsy_digital",
                "format": "digital_download",
                "sku_source": "etsy",
                "listing_type": "download",
                "config_hash": cfg_hash,
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.images_selected",
            payload={
                "draft_id": draft_id,
                "images": [{"path": "/mockups/single.jpg", "intent": "single", "rank": 1}],
                "sellable_file": {"source": "landing_original", "sha256": "abc", "bytes": 1},
            },
        ),
    )
    rebuild_listings(conn)
    return draft_id


def test_fill_forward_fills_missing_copy_and_price_without_new_created_event(conn, tmp_path):
    _seed_one(conn, tmp_path, file_id="e" * 64, photo_id="photo-e")
    draft_id = _seed_slice1_only_draft(
        conn, file_id="e" * 64, set_key=f"set-{'e' * 64}", photo_id="photo-e"
    )
    row = conn.execute(
        "SELECT title, price FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["title"] is None
    assert row["price"] is None

    result = build_drafts(conn, USER_ID)
    assert result.drafts_built == 1
    assert result.skipped_idempotent == 0

    created = [
        e
        for e in read_all(conn, "listingdraft.created")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    images_selected = [
        e
        for e in read_all(conn, "listingdraft.images_selected")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    copy_events = [
        e
        for e in read_all(conn, "listingdraft.copy_generated")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    priced_events = [
        e
        for e in read_all(conn, "listingdraft.priced")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    # created/images_selected are NOT re-emitted by fill-forward
    assert len(created) == 1
    assert len(images_selected) == 1
    assert len(copy_events) == 1
    assert len(priced_events) == 1


def test_fill_forward_second_run_is_a_no_op(conn, tmp_path):
    _seed_one(conn, tmp_path, file_id="d" * 64, photo_id="photo-d")
    draft_id = _seed_slice1_only_draft(
        conn, file_id="d" * 64, set_key=f"set-{'d' * 64}", photo_id="photo-d"
    )

    build_drafts(conn, USER_ID)  # fills forward
    second = build_drafts(conn, USER_ID)
    assert second.skipped_idempotent >= 1

    copy_events = [
        e
        for e in read_all(conn, "listingdraft.copy_generated")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    assert len(copy_events) == 1


def test_published_draft_is_never_rebuilt_even_with_force(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)

    draft_id = read_all(conn, "listingdraft.created")[0].payload["draft_id"]
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="gate3.published",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": "etsy-1",
                "state": "active",
                "published_at": "2026-07-14T00:00:00Z",
            },
        ),
    )
    result = build_drafts(conn, USER_ID, force=True)
    assert result.skipped_idempotent == 1
    assert result.drafts_built == 0
    assert len(read_all(conn, "listingdraft.created")) == 1
    assert len(read_all(conn, "listingdraft.copy_generated")) == 1


def test_fill_forward_copy_only_missing_leaves_price_alone(conn, tmp_path):
    _seed_one(conn, tmp_path, file_id="f" * 64, photo_id="photo-f")
    draft_id = _seed_slice1_only_draft(
        conn, file_id="f" * 64, set_key=f"set-{'f' * 64}", photo_id="photo-f"
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.priced",
            payload={
                "draft_id": draft_id,
                "format": "digital_download",
                "base_price": 7.99,
                "margin_floor": 4.99,
                "price": 7.99,
                "currency": "USD",
                "auto": True,
            },
        ),
    )
    rebuild_listings(conn)

    build_drafts(conn, USER_ID)

    copy_events = [
        e
        for e in read_all(conn, "listingdraft.copy_generated")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    priced_events = [
        e
        for e in read_all(conn, "listingdraft.priced")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    assert len(copy_events) == 1  # filled in
    assert len(priced_events) == 1  # pre-existing one, NOT re-emitted


def test_fill_forward_price_only_missing_leaves_copy_alone(conn, tmp_path):
    _seed_one(conn, tmp_path, file_id="a1" * 32, photo_id="photo-g")
    draft_id = _seed_slice1_only_draft(
        conn, file_id="a1" * 32, set_key=f"set-{'a1' * 32}", photo_id="photo-g"
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.copy_generated",
            payload={
                "draft_id": draft_id,
                "title": "Pre-existing Title",
                "tags": ["tag one"],
                "description": "Pre-existing description.",
                "model": "fixture",
                "provider": "fixture",
                "disclosure_appended": False,
            },
        ),
    )
    rebuild_listings(conn)

    build_drafts(conn, USER_ID)

    copy_events = [
        e
        for e in read_all(conn, "listingdraft.copy_generated")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    priced_events = [
        e
        for e in read_all(conn, "listingdraft.priced")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    assert len(copy_events) == 1  # pre-existing one, NOT re-emitted
    assert len(priced_events) == 1  # filled in
