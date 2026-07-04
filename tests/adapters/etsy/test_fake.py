from pathlib import Path

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter

FIXTURES = Path(__file__).parents[2] / "fixtures" / "etsy"


def test_fake_adapter_serves_fixture_data() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    assert adapter.get_shop().shop_name == "ExampleShop"
    assert len(adapter.list_listings()) == 3
    assert len(adapter.list_receipts()) == 2


def test_min_created_filters_receipts() -> None:
    adapter = FixtureEtsyAdapter(FIXTURES)
    assert [r.receipt_id for r in adapter.list_receipts(min_created=1751400000)] == [9002]
