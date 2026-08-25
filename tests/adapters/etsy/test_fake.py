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


def test_get_listing_inventory_returns_fixture_rows() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    inventory = adapter.get_listing_inventory(111)
    assert len(inventory.products) == 1
    product = inventory.products[0]
    assert product.product_id == 5001
    offering = product.offerings[0]
    assert offering.offering_id == 6001
    assert offering.price == 12.0  # Money{1200,100} normalized to a float
    assert offering.quantity == 5


def test_get_listing_inventory_unknown_listing_returns_empty() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    inventory = adapter.get_listing_inventory(999999)
    assert inventory.products == []


def test_list_shop_sections_returns_fixture_rows() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    sections = adapter.list_shop_sections()
    assert [s.title for s in sections] == ["Wildlife", "National Parks"]
    assert sections[0].shop_section_id == 100002


def test_list_taxonomy_nodes_returns_fixture_rows() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    nodes = adapter.list_taxonomy_nodes()
    assert len(nodes) == 1
    assert nodes[0].name == "Art & Collectibles"
    assert nodes[0].children[0].name == "Photography"


def test_find_active_listings_returns_fixture_page() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    page = adapter.find_active_listings("sandhill crane print")
    assert page.count == 5
    assert len(page.results) == 4  # count is the total on Etsy, not the returned page size
    assert page.results[0].listing_id == 111


def test_find_active_listings_limit_truncates_results() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    page = adapter.find_active_listings("sandhill crane print", limit=2)
    assert page.count == 5  # count is unaffected by limit
    assert len(page.results) == 2


def test_find_active_listings_unknown_phrase_returns_empty() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    page = adapter.find_active_listings("a phrase with zero fixture coverage")
    assert page.count == 0
    assert page.results == []
