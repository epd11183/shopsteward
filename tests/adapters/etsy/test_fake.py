from pathlib import Path

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter

FIXTURES = Path(__file__).parents[2] / "fixtures" / "etsy"


def test_fake_adapter_serves_fixture_data() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    assert adapter.get_shop().shop_name == "ExampleShop"
    assert len(adapter.list_listings()) == 7
    assert len(adapter.list_receipts()) == 10


def test_min_created_filters_receipts() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    assert [r.receipt_id for r in adapter.list_receipts(min_created=1755406400)] == [9012, 9013]


def test_get_listing_images_returns_fixture_rows() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    images = adapter.get_listing_images(111)
    assert len(images) == 1
    assert images[0].listing_image_id == 9001
    assert images[0].rank == 1


def test_get_listing_images_unknown_listing_returns_empty() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    assert adapter.get_listing_images(999999) == []


def test_list_reviews_returns_fixture_rows() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    reviews = adapter.list_reviews()
    assert len(reviews) == 2
    assert reviews[0].listing_id == 111
    assert reviews[0].rating == 5
    assert reviews[1].review == ""


def test_download_image_reads_fixture_bytes() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    images = adapter.get_listing_images(111)
    data = adapter.download_image(images[0].url_570xN)
    assert data == (FIXTURES / "sample_listing.jpg").read_bytes()
