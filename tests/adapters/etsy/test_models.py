from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt


def test_listing_parses_minimal_etsy_shape() -> None:
    listing = EtsyListing.model_validate(
        {
            "listing_id": 111,
            "title": "Misty Ridge Print",
            "state": "active",
            "quantity": 5,
            "views": 120,
            "num_favorers": 7,
            "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
            "tags": ["landscape", "wall art"],
        }
    )
    assert listing.price_usd == 25.0
    assert listing.description is None  # never carried by this old-shaped payload


def test_listing_validates_an_old_stored_payload_missing_description_entirely() -> None:
    """Every already-stored `etsy.listing.observed` event predates the
    `description` field and has no such key at all -- model_validate() on
    that exact old shape must still succeed and yield description=None (the
    backward-compat guarantee `listing.seo_edit`'s description-editing
    baseline rule rests on), no migration, no touching the immutable
    stored event."""
    old_shaped_payload = {
        "listing_id": 111,
        "title": "Misty Ridge Print",
        "state": "active",
        "quantity": 5,
        "views": 120,
        "num_favorers": 7,
        "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
        "tags": ["landscape", "wall art"],
        "should_auto_renew": True,
    }
    assert "description" not in old_shaped_payload

    listing = EtsyListing.model_validate(old_shaped_payload)

    assert listing.description is None


def test_receipt_totals() -> None:
    receipt = EtsyReceipt.model_validate(
        {
            "receipt_id": 9,
            "created_timestamp": 1751500000,
            "grandtotal": {"amount": 4300, "divisor": 100, "currency_code": "USD"},
            "transactions": [
                {
                    "transaction_id": 1,
                    "listing_id": 111,
                    "quantity": 1,
                    "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
                },
            ],
        }
    )
    assert receipt.total_usd == 43.0
    assert receipt.transactions[0].listing_id == 111
