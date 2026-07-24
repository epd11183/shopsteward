import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.adapters.etsy.models import EtsyDraftSpec, EtsyListingUpdate


def _spec(**overrides: object) -> EtsyDraftSpec:
    base = dict(
        quantity=999,
        title="Sunset Over the Bay",
        description="A digital download.",
        price=12.00,
        who_made="i_did",
        when_made="2020_2026",
        taxonomy_id=0,
        tags=["wall art", "coastal"],
    )
    base.update(overrides)
    return EtsyDraftSpec(**base)


def test_create_draft_listing_starts_state_draft() -> None:
    adapter = FakeEtsyWriteAdapter()
    ref = adapter.create_draft_listing(_spec())
    assert ref.state == "draft"
    assert ref.listing_id == 1000


def test_create_draft_listing_ids_are_sequential() -> None:
    adapter = FakeEtsyWriteAdapter()
    first = adapter.create_draft_listing(_spec())
    second = adapter.create_draft_listing(_spec())
    assert second.listing_id == first.listing_id + 1


def test_upload_image_before_create_raises() -> None:
    adapter = FakeEtsyWriteAdapter()
    with pytest.raises(EtsyWriteError) as exc_info:
        adapter.upload_listing_image(9999, b"jpeg-bytes", rank=1)
    assert exc_info.value.status_code == 404


def test_upload_file_before_create_raises() -> None:
    adapter = FakeEtsyWriteAdapter()
    with pytest.raises(EtsyWriteError) as exc_info:
        adapter.upload_listing_file(9999, b"file-bytes", name="a.jpg", rank=1)
    assert exc_info.value.status_code == 404


def test_update_unknown_listing_raises() -> None:
    adapter = FakeEtsyWriteAdapter()
    with pytest.raises(EtsyWriteError) as exc_info:
        adapter.update_listing(9999, EtsyListingUpdate(title="New title"))
    assert exc_info.value.status_code == 404


def test_publish_unknown_listing_raises() -> None:
    adapter = FakeEtsyWriteAdapter()
    with pytest.raises(EtsyWriteError) as exc_info:
        adapter.publish_listing(9999)
    assert exc_info.value.status_code == 404


def test_publish_without_image_or_file_is_rejected() -> None:
    adapter = FakeEtsyWriteAdapter()
    ref = adapter.create_draft_listing(_spec())

    with pytest.raises(EtsyWriteError, match="zero images"):
        adapter.publish_listing(ref.listing_id)

    adapter.upload_listing_image(ref.listing_id, b"jpeg", rank=1)
    with pytest.raises(EtsyWriteError, match="zero digital files"):
        adapter.publish_listing(ref.listing_id)


def test_publish_with_image_and_file_flips_state_active() -> None:
    adapter = FakeEtsyWriteAdapter()
    ref = adapter.create_draft_listing(_spec())
    adapter.upload_listing_image(ref.listing_id, b"jpeg", rank=1)
    adapter.upload_listing_file(ref.listing_id, b"file", name="art.jpg", rank=1)

    listing = adapter.publish_listing(ref.listing_id)
    assert listing.state == "active"
    assert adapter.listings[ref.listing_id]["state"] == "active"


def test_update_listing_applies_fields_and_is_visible_via_return_value() -> None:
    adapter = FakeEtsyWriteAdapter()
    ref = adapter.create_draft_listing(_spec())

    updated = adapter.update_listing(
        ref.listing_id,
        EtsyListingUpdate(title="New title", tags=["new tag"]),
    )
    assert updated.title == "New title"
    assert updated.tags == ["new tag"]
    assert updated.state == "draft"  # update_listing never touches state


def test_delete_listing_removes_and_then_rejects_writes() -> None:
    adapter = FakeEtsyWriteAdapter()
    ref = adapter.create_draft_listing(_spec())
    adapter.delete_listing(ref.listing_id)

    with pytest.raises(EtsyWriteError):
        adapter.update_listing(ref.listing_id, EtsyListingUpdate(title="x"))


def test_calls_log_records_every_invocation() -> None:
    adapter = FakeEtsyWriteAdapter()
    ref = adapter.create_draft_listing(_spec())
    adapter.upload_listing_image(ref.listing_id, b"jpeg", rank=1)
    adapter.upload_listing_file(ref.listing_id, b"file", name="art.jpg", rank=1)

    names = [name for name, _ in adapter.calls]
    assert names == ["create_draft_listing", "upload_listing_image", "upload_listing_file"]
