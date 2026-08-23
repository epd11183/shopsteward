import pytest
from PIL import Image

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings import gate3
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.pricing import BelowFloor
from shopsteward.pipeline.listings.push import push_drafts

from .helpers import USER_ID, seed_fully_built_draft, seed_landing_file_with_mockup_set


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _build_one_pushed_draft(
    conn, tmp_path, adapter, *, file_id="f" * 64, photo_id="photo-1"
) -> str:
    """Builds + pushes one draft using the given adapter (build_drafts's
    etsy_adapter injection point) -- callers keep a reference to `adapter` so
    later gate3.edit/publish/retry_push calls see the same in-memory Fake
    listing, mirroring how the API layer shares one adapter instance across
    a build request and the Gate 3 requests that follow it."""
    path = tmp_path / f"{photo_id}.jpg"
    Image.new("RGB", (100, 100), (5, 6, 7)).save(path, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id=file_id,
        photo_id=photo_id,
        path=str(path),
        set_key=f"set-{file_id}",
        intents=["single", "digital_whatyougot"],
        mockups_dir=tmp_path / "mockups",
    )
    report = build_drafts(conn, USER_ID, etsy_adapter=adapter)
    assert report.pushed == 1
    row = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
    ).fetchone()
    return row["draft_id"]


# --- queue -------------------------------------------------------------


def test_queue_shape_includes_economics_and_images(conn, tmp_path):
    draft_id = _build_one_pushed_draft(conn, tmp_path, FakeEtsyWriteAdapter())

    cards = gate3.queue(conn, USER_ID)
    assert len(cards) == 1
    card = cards[0]
    assert card.draft_id == draft_id
    assert card.state == "pushed"
    assert card.etsy_listing_id is not None
    assert card.price == 12.00
    assert card.margin_floor == 6.00
    assert card.economics is not None
    assert card.economics.price == 12.00
    # etsy_fees per config/defaults/listing.json: 0.20 + 12*0.065 + 12*0.03+0.25
    assert card.economics.etsy_fees == pytest.approx(0.20 + 0.78 + 0.61, abs=0.01)
    assert card.economics.net == pytest.approx(12.00 - card.economics.etsy_fees, abs=0.01)
    assert len(card.images) == 2
    assert card.images[0].rank == 1
    assert card.file_source == "landing_original"
    assert card.retry_error is None


def test_queue_excludes_built_and_published_drafts(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    gate3.publish(conn, USER_ID, draft_id, adapter)  # publish -> out of the queue

    assert gate3.queue(conn, USER_ID) == []


def test_queue_includes_push_failed_with_retry_error(conn, tmp_path):
    draft_id = seed_fully_built_draft(
        conn, tmp_path, file_id="a" * 64, photo_id="photo-a", title="Broken", set_key="set-a"
    )
    cfg = listing_config.get_config(conn, USER_ID)

    class _FailingAdapter(FakeEtsyWriteAdapter):
        def create_draft_listing(self, spec):
            raise EtsyWriteError(500, "boom")

    result = push_drafts(conn, USER_ID, cfg, _FailingAdapter())
    assert result.push_failed == 1

    cards = gate3.queue(conn, USER_ID)
    assert len(cards) == 1
    assert cards[0].draft_id == draft_id
    assert cards[0].state == "push_failed"
    assert cards[0].etsy_listing_id is None
    assert cards[0].retry_error == "Etsy write failed with HTTP 500: boom"


# --- edit ----------------------------------------------------------------


def test_edit_updates_title_tags_description_and_calls_adapter(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    calls_before = len(adapter.calls)

    card = gate3.edit(
        conn,
        USER_ID,
        draft_id,
        {"title": "New Title", "tags": ["wall art", "cabin"], "description": "New desc"},
        adapter,
    )

    assert card.title == "New Title"
    assert card.tags == ["wall art", "cabin"]
    assert card.description == "New desc"

    new_calls = adapter.calls[calls_before:]
    update_calls = [c for c in new_calls if c[0] == "update_listing"]
    assert len(update_calls) == 1
    assert update_calls[0][1]["fields"]["title"] == "New Title"
    assert update_calls[0][1]["fields"]["tags"] == ["wall art", "cabin"]
    # price is never sent to update_listing -- Etsy's real endpoint has no
    # price field (PRD §13 decision 41 / models.py EtsyListingUpdate).
    assert "price" not in update_calls[0][1]["fields"]

    events = [e for e in read_all(conn, "listingdraft.edited") if e.user_id == USER_ID]
    assert len(events) == 1
    assert events[0].payload["fields"]["title"] == "New Title"
    assert events[0].payload["price"] is None


def test_edit_price_calls_update_listing_price_not_update_listing(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    calls_before = len(adapter.calls)

    card = gate3.edit(conn, USER_ID, draft_id, {"price": 9.50}, adapter)

    assert card.price == 9.50
    new_calls = adapter.calls[calls_before:]
    assert [c[0] for c in new_calls] == ["update_listing_price"]
    assert new_calls[0][1] == {"listing_id": int(card.etsy_listing_id), "price": 9.50}
    assert adapter.listings[int(card.etsy_listing_id)]["price"] == 9.50

    events = [e for e in read_all(conn, "listingdraft.edited") if e.user_id == USER_ID]
    assert events[0].payload["price"] == 9.50
    assert events[0].payload["fields"] == {}


def test_edit_price_and_title_together_calls_both_adapter_methods(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    calls_before = len(adapter.calls)

    gate3.edit(conn, USER_ID, draft_id, {"title": "New Title", "price": 9.50}, adapter)

    new_call_names = {c[0] for c in adapter.calls[calls_before:]}
    assert new_call_names == {"update_listing", "update_listing_price"}


def test_publish_after_price_edit_sees_new_price_on_fake(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    gate3.edit(conn, USER_ID, draft_id, {"price": 9.50}, adapter)
    card = gate3.publish(conn, USER_ID, draft_id, adapter)

    assert card.state == "published"
    assert adapter.listings[int(card.etsy_listing_id)]["price"] == 9.50


def test_edit_price_below_floor_rejected(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    calls_before = len(adapter.calls)

    with pytest.raises(BelowFloor):
        gate3.edit(conn, USER_ID, draft_id, {"price": 1.00}, adapter)

    # no event, no adapter call, no projection change
    assert read_all(conn, "listingdraft.edited") == []
    assert len(adapter.calls) == calls_before
    row = conn.execute(
        "SELECT price FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["price"] == 12.00


def test_edit_rejects_too_many_tags(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, draft_id, {"tags": [f"tag{i}" for i in range(14)]}, adapter)


def test_edit_rejects_tag_over_20_chars(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, draft_id, {"tags": ["x" * 21]}, adapter)


def test_edit_rejects_empty_tag(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, draft_id, {"tags": ["  "]}, adapter)


def test_edit_rejects_tag_containing_comma(conn, tmp_path):
    # Etsy's write path comma-joins tags into a single form field
    # (adapters/etsy/live.py::_encode_form_data) -- a tag containing a
    # literal comma would silently split into extra tags on the wire,
    # undetectable server-side.
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, draft_id, {"tags": ["black, white", "red"]}, adapter)


def test_edit_no_fields_is_an_error(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, draft_id, {}, adapter)


def test_edit_unknown_draft_is_rejected(conn):
    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, "does-not-exist", {"title": "x"}, FakeEtsyWriteAdapter())


def test_edit_unpushed_draft_is_rejected(conn, tmp_path):
    draft_id = seed_fully_built_draft(
        conn, tmp_path, file_id="z" * 64, photo_id="photo-z", title="Never Pushed", set_key="set-z"
    )
    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, draft_id, {"title": "x"}, FakeEtsyWriteAdapter())


def test_edit_published_draft_is_rejected(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    gate3.publish(conn, USER_ID, draft_id, adapter)
    calls_before = len(adapter.calls)

    with pytest.raises(ValueError):
        gate3.edit(conn, USER_ID, draft_id, {"title": "x"}, adapter)

    assert read_all(conn, "listingdraft.edited") == []
    assert len(adapter.calls) == calls_before  # no adapter call either


# --- publish ---------------------------------------------------------------


def test_publish_happy_path_event_sequence(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    card = gate3.publish(conn, USER_ID, draft_id, adapter)

    assert card.state == "published"
    approved = [e for e in read_all(conn, "gate3.approved") if e.user_id == USER_ID]
    published = [e for e in read_all(conn, "gate3.published") if e.user_id == USER_ID]
    assert len(approved) == 1
    assert len(published) == 1
    assert approved[0].payload["draft_id"] == draft_id
    assert published[0].payload["draft_id"] == draft_id
    assert published[0].payload["state"] == "active"
    assert published[0].payload["published_at"]

    publish_calls = [c for c in adapter.calls if c[0] == "publish_listing"]
    assert len(publish_calls) == 1

    row = conn.execute(
        "SELECT state, published_at FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["state"] == "published"
    assert row["published_at"]


def test_publish_is_the_sole_call_site_for_publish_listing():
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[3] / "src" / "shopsteward"
    callers = []
    for py_file in src_root.rglob("*.py"):
        if py_file.name == "gate3.py":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "publish_listing"
                and not py_file.name.endswith(("fake.py", "interface.py", "live.py"))
            ):
                callers.append(str(py_file))
    assert callers == []


def test_publish_adapter_failure_emits_publish_failed_and_returns_card(conn, tmp_path):
    class _FailingPublishAdapter(FakeEtsyWriteAdapter):
        def publish_listing(self, listing_id):
            raise EtsyWriteError(500, "etsy is down")

    adapter = _FailingPublishAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    card = gate3.publish(conn, USER_ID, draft_id, adapter)

    assert card.state == "publish_failed"
    assert card.retry_error == "Etsy write failed with HTTP 500: etsy is down"

    approved = [e for e in read_all(conn, "gate3.approved") if e.user_id == USER_ID]
    failed = [e for e in read_all(conn, "gate3.publish_failed") if e.user_id == USER_ID]
    published = [e for e in read_all(conn, "gate3.published") if e.user_id == USER_ID]
    assert len(approved) == 1
    assert len(failed) == 1
    assert published == []


def test_publish_unknown_draft_rejected(conn, tmp_path):
    with pytest.raises(ValueError):
        gate3.publish(conn, USER_ID, "does-not-exist", FakeEtsyWriteAdapter())


def test_publish_unpushed_draft_rejected(conn, tmp_path):
    draft_id = seed_fully_built_draft(
        conn, tmp_path, file_id="y" * 64, photo_id="photo-y", title="Never Pushed", set_key="set-y"
    )
    with pytest.raises(ValueError):
        gate3.publish(conn, USER_ID, draft_id, FakeEtsyWriteAdapter())


def test_publish_retries_after_publish_failed(conn, tmp_path):
    class _FailOnceAdapter(FakeEtsyWriteAdapter):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def publish_listing(self, listing_id):
            if self.fail_next:
                self.fail_next = False
                raise EtsyWriteError(500, "transient")
            return super().publish_listing(listing_id)

    adapter = _FailOnceAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)

    first = gate3.publish(conn, USER_ID, draft_id, adapter)
    assert first.state == "publish_failed"

    second = gate3.publish(conn, USER_ID, draft_id, adapter)
    assert second.state == "published"


# --- retry_push --------------------------------------------------------


def test_retry_push_full_rebuild_when_create_never_succeeded(conn, tmp_path):
    draft_id = seed_fully_built_draft(
        conn, tmp_path, file_id="a" * 64, photo_id="photo-a", title="Retry Me", set_key="set-a"
    )
    cfg = listing_config.get_config(conn, USER_ID)

    class _FailCreateAdapter(FakeEtsyWriteAdapter):
        def create_draft_listing(self, spec):
            raise EtsyWriteError(500, "boom")

    first = push_drafts(conn, USER_ID, cfg, _FailCreateAdapter())
    assert first.push_failed == 1
    row = conn.execute(
        "SELECT etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["etsy_listing_id"] is None

    card = gate3.retry_push(conn, USER_ID, draft_id, FakeEtsyWriteAdapter())
    assert card.state == "pushed"
    assert card.etsy_listing_id is not None


def test_retry_push_resumes_from_failed_image_stage(conn, tmp_path):
    draft_id = seed_fully_built_draft(
        conn, tmp_path, file_id="b" * 64, photo_id="photo-b", title="Resume Me", set_key="set-b"
    )
    cfg = listing_config.get_config(conn, USER_ID)

    class _FailImageAdapter(FakeEtsyWriteAdapter):
        def upload_listing_image(self, listing_id, image, *, rank):
            raise EtsyWriteError(502, "image upload failed")

    fail_adapter = _FailImageAdapter()
    result = push_drafts(conn, USER_ID, cfg, fail_adapter)
    assert result.push_failed == 1

    row = conn.execute(
        "SELECT state, etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["state"] == "push_failed"
    assert row["etsy_listing_id"] is not None  # create succeeded before the image failed

    # Resume with the SAME (in-memory) adapter, minus the failing method --
    # proves resume reuses the existing listing_id instead of re-creating.
    ok_adapter = fail_adapter
    ok_adapter.upload_listing_image = FakeEtsyWriteAdapter.upload_listing_image.__get__(ok_adapter)

    create_calls_before = len([c for c in ok_adapter.calls if c[0] == "create_draft_listing"])
    card = gate3.retry_push(conn, USER_ID, draft_id, ok_adapter)

    assert card.state == "pushed"
    create_calls_after = len([c for c in ok_adapter.calls if c[0] == "create_draft_listing"])
    assert create_calls_after == create_calls_before  # never re-created

    resumed = [e for e in read_all(conn, "listingdraft.push_resumed") if e.user_id == USER_ID]
    assert len(resumed) == 1
    assert resumed[0].payload["draft_id"] == draft_id


def test_retry_push_resume_skips_already_attached_images(conn, tmp_path):
    """Fail on image 2 of 2 -> resume must upload ONLY image 2 (no duplicate
    rank-1 upload). listingdraft.images_attached is now emitted once per
    image, so the already-attached rank is derivable from prior events."""
    draft_id = seed_fully_built_draft(
        conn,
        tmp_path,
        file_id="d" * 64,
        photo_id="photo-two",
        title="Two Images",
        set_key="set-two",
        intents=["single", "digital_whatyougot"],
    )
    cfg = listing_config.get_config(conn, USER_ID)

    class _FailSecondImageAdapter(FakeEtsyWriteAdapter):
        def __init__(self):
            super().__init__()
            self.failed_rank_2_once = False

        def upload_listing_image(self, listing_id, image, *, rank):
            if rank == 2 and not self.failed_rank_2_once:
                self.failed_rank_2_once = True
                raise EtsyWriteError(502, "second image failed")
            return super().upload_listing_image(listing_id, image, rank=rank)

    fail_adapter = _FailSecondImageAdapter()
    result = push_drafts(conn, USER_ID, cfg, fail_adapter)
    assert result.push_failed == 1

    attached = [
        e
        for e in read_all(conn, "listingdraft.images_attached")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    assert len(attached) == 1  # only rank 1 made it before rank 2 failed
    assert attached[0].payload["images"][0]["rank"] == 1

    calls_before = len(fail_adapter.calls)  # index into the full call log, not a filtered count
    card = gate3.retry_push(conn, USER_ID, draft_id, fail_adapter)
    assert card.state == "pushed"

    new_calls = fail_adapter.calls[calls_before:]
    image_upload_ranks = [c[1]["rank"] for c in new_calls if c[0] == "upload_listing_image"]
    assert image_upload_ranks == [2]  # rank 1 never re-uploaded

    attached_after = [
        e
        for e in read_all(conn, "listingdraft.images_attached")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    assert len(attached_after) == 2  # one event per image, accumulated across attempts
    assert {img["rank"] for e in attached_after for img in e.payload["images"]} == {1, 2}


def test_retry_push_resumes_from_failed_file_stage(conn, tmp_path):
    draft_id = seed_fully_built_draft(
        conn, tmp_path, file_id="c" * 64, photo_id="photo-c", title="Resume File", set_key="set-c"
    )
    cfg = listing_config.get_config(conn, USER_ID)

    class _FailFileAdapter(FakeEtsyWriteAdapter):
        def upload_listing_file(self, listing_id, file, *, name, rank):
            raise EtsyWriteError(502, "file upload failed")

    fail_adapter = _FailFileAdapter()
    result = push_drafts(conn, USER_ID, cfg, fail_adapter)
    assert result.push_failed == 1

    fail_adapter.upload_listing_file = FakeEtsyWriteAdapter.upload_listing_file.__get__(
        fail_adapter
    )
    card = gate3.retry_push(conn, USER_ID, draft_id, fail_adapter)
    assert card.state == "pushed"

    images_attached = [
        e
        for e in read_all(conn, "listingdraft.images_attached")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    # images stage was never re-run (it already succeeded before the file
    # stage failed)
    assert len(images_attached) == 1


def test_retry_push_rejects_non_push_failed_draft(conn, tmp_path):
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    with pytest.raises(ValueError):
        gate3.retry_push(conn, USER_ID, draft_id, adapter)


def test_retry_push_unknown_draft_rejected(conn):
    with pytest.raises(ValueError):
        gate3.retry_push(conn, USER_ID, "nope", FakeEtsyWriteAdapter())
