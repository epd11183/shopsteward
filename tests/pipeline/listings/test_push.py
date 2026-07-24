import pytest
from PIL import Image

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.adapters.etsy.models import (
    EtsyDraftSpec,
    EtsyFileRef,
    EtsyImageRef,
    EtsyListingRef,
)
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.push import push_drafts

from .helpers import USER_ID, seed_fully_built_draft, seed_landing_file_with_mockup_set


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
        intents=["single", "digital_whatyougot"],
        mockups_dir=tmp_path / "mockups",
    )
    return path


def _cfg(conn):
    return listing_config.get_config(conn, USER_ID)


def test_push_happy_path_event_sequence_and_payloads(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)  # build already folds push (Fake adapter)

    pushed = [e for e in read_all(conn, "listingdraft.pushed_to_etsy") if e.user_id == USER_ID]
    images_attached = [
        e for e in read_all(conn, "listingdraft.images_attached") if e.user_id == USER_ID
    ]
    file_attached = [
        e for e in read_all(conn, "listingdraft.file_attached") if e.user_id == USER_ID
    ]
    assert len(pushed) == 1
    # one listingdraft.images_attached event PER image (reviewer fix-up,
    # M5a slice 4) -- not one event for the whole batch.
    assert len(images_attached) == 2
    assert len(file_attached) == 1

    draft_id = pushed[0].payload["draft_id"]
    assert all(e.payload["draft_id"] == draft_id for e in images_attached)
    assert file_attached[0].payload["draft_id"] == draft_id

    push_payload = pushed[0].payload
    assert push_payload["listing_type"] == "download"
    assert push_payload["quantity"] == 999
    assert push_payload["state"] == "draft"
    assert push_payload["etsy_listing_id"] == 1000

    images = [img for e in images_attached for img in e.payload["images"]]
    assert len(images) == 2
    assert images[0]["rank"] == 1
    assert images[0]["intent"] == "single"
    assert {img["intent"] for img in images} == {"single", "digital_whatyougot"}

    file_payload = file_attached[0].payload
    assert file_payload["source"] == "landing_original"
    assert file_payload["sha256"]

    row = conn.execute(
        "SELECT state, etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["state"] == "pushed"
    assert row["etsy_listing_id"] == "1000"


def test_push_is_idempotent_pushed_draft_never_repushed(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)
    first_pushed = [
        e for e in read_all(conn, "listingdraft.pushed_to_etsy") if e.user_id == USER_ID
    ]
    assert len(first_pushed) == 1

    cfg = _cfg(conn)
    second = push_drafts(conn, USER_ID, cfg, FakeEtsyWriteAdapter())
    assert second.pushed == 0
    assert second.push_failed == 0

    still_pushed = [
        e for e in read_all(conn, "listingdraft.pushed_to_etsy") if e.user_id == USER_ID
    ]
    assert len(still_pushed) == 1  # no duplicate create


def test_force_rebuild_does_not_repush(conn, tmp_path):
    _seed_one(conn, tmp_path)
    build_drafts(conn, USER_ID)
    forced = build_drafts(conn, USER_ID, force=True)

    assert forced.pushed == 0  # already pushed; force never re-pushes
    pushed = [e for e in read_all(conn, "listingdraft.pushed_to_etsy") if e.user_id == USER_ID]
    assert len(pushed) == 1


# --- push_drafts() exercised directly against hand-built rows --------------
#
# build_drafts() always folds push in (design §1 dataflow); to exercise
# push_drafts()'s own batch-isolation behavior in a controlled way, these
# tests build the four upstream events (created/images_selected/
# copy_generated/priced) directly -- the same shape build_drafts() would
# have emitted -- then call push_drafts() with a hand-picked adapter,
# without ever letting build_drafts's own auto-push run first.


def _seed_fully_built_draft(
    conn, tmp_path, *, file_id: str, photo_id: str, title: str, set_key: str
) -> str:
    return seed_fully_built_draft(
        conn, tmp_path, file_id=file_id, photo_id=photo_id, title=title, set_key=set_key
    )


class _FailFirstCreateAdapter:
    """Wraps a FakeEtsyWriteAdapter but raises on create_draft_listing for
    one chosen title, to exercise mid-batch failure isolation."""

    def __init__(self, fail_title: str):
        self.inner = FakeEtsyWriteAdapter()
        self._fail_title = fail_title

    def create_draft_listing(self, spec: EtsyDraftSpec) -> EtsyListingRef:
        if spec.title == self._fail_title:
            raise EtsyWriteError(500, "simulated create failure")
        return self.inner.create_draft_listing(spec)

    def upload_listing_image(self, listing_id: int, image: bytes, *, rank: int) -> EtsyImageRef:
        return self.inner.upload_listing_image(listing_id, image, rank=rank)

    def upload_listing_file(
        self, listing_id: int, file: bytes, *, name: str, rank: int
    ) -> EtsyFileRef:
        return self.inner.upload_listing_file(listing_id, file, name=name, rank=rank)

    def update_listing(self, listing_id, fields):
        return self.inner.update_listing(listing_id, fields)

    def publish_listing(self, listing_id):
        return self.inner.publish_listing(listing_id)

    def delete_listing(self, listing_id):
        return self.inner.delete_listing(listing_id)


def test_mid_batch_failure_continues_to_next_draft(conn, tmp_path):
    draft_a = _seed_fully_built_draft(
        conn, tmp_path, file_id="a" * 64, photo_id="photo-a", title="Fails", set_key="set-a"
    )
    draft_b = _seed_fully_built_draft(
        conn, tmp_path, file_id="b" * 64, photo_id="photo-b", title="Succeeds", set_key="set-b"
    )

    adapter = _FailFirstCreateAdapter(fail_title="Fails")
    result = push_drafts(conn, USER_ID, _cfg(conn), adapter)

    assert result.pushed == 1
    assert result.push_failed == 1

    failed_events = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID]
    assert len(failed_events) == 1
    assert failed_events[0].payload["draft_id"] == draft_a
    assert failed_events[0].payload["stage"] == "create"
    assert failed_events[0].payload["etsy_listing_id"] is None
    assert failed_events[0].payload["error"]["code"] == 500

    pushed_events = [
        e for e in read_all(conn, "listingdraft.pushed_to_etsy") if e.user_id == USER_ID
    ]
    assert len(pushed_events) == 1
    assert pushed_events[0].payload["draft_id"] == draft_b

    row_a = conn.execute(
        "SELECT state, etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_a),
    ).fetchone()
    assert row_a["state"] == "push_failed"
    assert row_a["etsy_listing_id"] is None

    row_b = conn.execute(
        "SELECT state, etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_b),
    ).fetchone()
    assert row_b["state"] == "pushed"
    assert row_b["etsy_listing_id"] is not None


def test_failure_after_create_records_etsy_listing_id_in_push_failed(conn, tmp_path):
    draft_id = _seed_fully_built_draft(
        conn, tmp_path, file_id="c" * 64, photo_id="photo-c", title="Partial", set_key="set-c"
    )

    class _FailImageAdapter(_FailFirstCreateAdapter):
        def __init__(self):
            super().__init__(fail_title="__never__")

        def upload_listing_image(self, listing_id, image, *, rank):
            raise EtsyWriteError(502, "simulated image upload failure")

    result = push_drafts(conn, USER_ID, _cfg(conn), _FailImageAdapter())
    assert result.pushed == 0
    assert result.push_failed == 1

    failed = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID][0]
    assert failed.payload["stage"] == "image"
    assert failed.payload["etsy_listing_id"] == 1000  # create succeeded before the image failed

    row = conn.execute(
        "SELECT state, etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["state"] == "push_failed"
    assert row["etsy_listing_id"] == "1000"

    # A create-succeeded-but-later-stage-failed draft is left for Gate 3's
    # retry (M5a slice 4) -- push_drafts never retries it automatically.
    second = push_drafts(conn, USER_ID, _cfg(conn), FakeEtsyWriteAdapter())
    assert second.pushed == 0
    assert second.push_failed == 0


def test_published_draft_is_never_pushed(conn, tmp_path):
    draft_id = _seed_fully_built_draft(
        conn, tmp_path, file_id="d" * 64, photo_id="photo-d", title="Published", set_key="set-d"
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="gate3.published",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": "already-live",
                "state": "active",
                "published_at": "2026-07-14T00:00:00Z",
            },
        ),
    )
    rebuild_listings(conn)

    result = push_drafts(conn, USER_ID, _cfg(conn), FakeEtsyWriteAdapter())
    assert result.pushed == 0
    assert result.push_failed == 0
    assert read_all(conn, "listingdraft.pushed_to_etsy") == []


def test_create_stage_failure_is_retriable_on_next_push(conn, tmp_path):
    draft_id = _seed_fully_built_draft(
        conn, tmp_path, file_id="d" * 64, photo_id="photo-d", title="Retry Me", set_key="set-d"
    )

    first = push_drafts(conn, USER_ID, _cfg(conn), _FailFirstCreateAdapter(fail_title="Retry Me"))
    assert first.push_failed == 1 and first.pushed == 0

    second = push_drafts(conn, USER_ID, _cfg(conn), FakeEtsyWriteAdapter())
    assert second.pushed == 1 and second.push_failed == 0

    pushed = [
        e
        for e in read_all(conn, "listingdraft.pushed_to_etsy")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_id
    ]
    assert len(pushed) == 1
    row = conn.execute(
        "SELECT state FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["state"] == "pushed"


def test_missing_local_image_file_is_per_draft_failure_not_batch_abort(conn, tmp_path):
    from pathlib import Path as _Path

    draft_a = _seed_fully_built_draft(
        conn, tmp_path, file_id="e1" * 32, photo_id="photo-e1", title="Broken", set_key="set-e1"
    )
    _seed_fully_built_draft(
        conn, tmp_path, file_id="e2" * 32, photo_id="photo-e2", title="Fine", set_key="set-e2"
    )

    # break draft A's first image path on disk
    ev = next(
        e
        for e in read_all(conn, "listingdraft.images_selected")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_a
    )
    _Path(ev.payload["images"][0]["path"]).unlink()

    result = push_drafts(conn, USER_ID, _cfg(conn), FakeEtsyWriteAdapter())
    assert result.push_failed == 1
    assert result.pushed == 1  # draft B still went through

    failed = [
        e
        for e in read_all(conn, "listingdraft.push_failed")
        if e.user_id == USER_ID and e.payload["draft_id"] == draft_a
    ]
    assert len(failed) == 1
    assert failed[0].payload["stage"] == "image"
    assert failed[0].payload["error"]["code"] == 0  # local IO, not an Etsy HTTP error
