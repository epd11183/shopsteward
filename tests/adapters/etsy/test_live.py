import time
import urllib.parse

import httpx
import pytest
import respx

from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.adapters.etsy.live import LiveEtsyAdapter, LiveEtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyDraftSpec, EtsyListingUpdate

BASE = "https://openapi.etsy.com/v3/application"


@respx.mock
def test_list_listings_paginates_and_parses() -> None:
    respx.get(f"{BASE}/shops/100001/listings/active").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "results": [
                    {
                        "listing_id": 111,
                        "title": "T",
                        "state": "active",
                        "quantity": 1,
                        "price": {"amount": 100, "divisor": 100, "currency_code": "USD"},
                    }
                ],
            },
        )
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    listings = adapter.list_listings()
    assert listings[0].listing_id == 111
    sent = respx.calls.last.request
    assert sent.headers["x-api-key"] == "k"
    assert sent.headers["authorization"] == "Bearer tok"


@respx.mock
def test_pagination_follows_count() -> None:
    def pager(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request.url.params)["offset"])
        row = {
            "listing_id": offset + 1,
            "title": "T",
            "state": "active",
            "quantity": 1,
            "price": {"amount": 100, "divisor": 100, "currency_code": "USD"},
        }
        return httpx.Response(200, json={"count": 150, "results": [row]})

    respx.get(f"{BASE}/shops/100001/listings/active").mock(side_effect=pager)
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    assert len(adapter.list_listings()) == 2  # count=150 -> offsets 0 and 100


# --- LiveEtsyWriteAdapter ---------------------------------------------------


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


def _write_adapter(tmp_path) -> LiveEtsyWriteAdapter:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    store.save(
        EtsyTokens(
            access_token="fresh-access-token",
            access_expires_at=time.time() + 3600,
            refresh_token="refresh-secret",
            shop_id=100001,
            etsy_user_id=1234,
            scopes=["listings_r", "listings_w"],
        )
    )
    return LiveEtsyWriteAdapter(api_key="k", shop_id=100001, token_store=store)


@respx.mock
def test_create_draft_listing_sends_form_urlencoded_body(tmp_path) -> None:
    route = respx.post(f"{BASE}/shops/100001/listings").mock(
        return_value=httpx.Response(200, json={"listing_id": 555, "state": "draft"})
    )
    adapter = _write_adapter(tmp_path)
    ref = adapter.create_draft_listing(_spec())

    assert ref.listing_id == 555
    assert ref.state == "draft"
    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "k"
    assert sent.headers["authorization"] == "Bearer fresh-access-token"
    assert sent.headers["content-type"] == "application/x-www-form-urlencoded"

    body = dict(urllib.parse.parse_qsl(sent.content.decode()))
    assert body["quantity"] == "999"
    assert body["title"] == "Sunset Over the Bay"
    assert body["price"] == "12.0"
    assert body["who_made"] == "i_did"
    assert body["when_made"] == "2020_2026"
    assert body["taxonomy_id"] == "0"
    assert body["type"] == "download"
    assert body["is_supply"] == "false"
    assert body["should_auto_renew"] == "true"
    # tags is a repeated-key list, not a JSON array or a single field
    tags = [v for k, v in urllib.parse.parse_qsl(sent.content.decode()) if k == "tags"]
    assert tags == ["wall art", "coastal"]
    # createDraftListing has no `state` request field at all -- the
    # endpoint inherently creates drafts (Etsy OpenAPI spec).
    assert "state" not in body


@respx.mock
def test_create_draft_listing_rejects_non_draft_response(tmp_path) -> None:
    respx.post(f"{BASE}/shops/100001/listings").mock(
        return_value=httpx.Response(200, json={"listing_id": 555, "state": "active"})
    )
    adapter = _write_adapter(tmp_path)

    with pytest.raises(EtsyWriteError, match="expected draft"):
        adapter.create_draft_listing(_spec())


@respx.mock
def test_upload_listing_image_sends_multipart_with_rank(tmp_path) -> None:
    route = respx.post(f"{BASE}/shops/100001/listings/555/images").mock(
        return_value=httpx.Response(200, json={"listing_image_id": 77, "rank": 2})
    )
    adapter = _write_adapter(tmp_path)
    ref = adapter.upload_listing_image(555, b"\xff\xd8jpegbytes", rank=2)

    assert ref.listing_image_id == 77
    assert ref.rank == 2
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"jpegbytes" in sent.content


@respx.mock
def test_upload_listing_file_sends_multipart_with_name(tmp_path) -> None:
    route = respx.post(f"{BASE}/shops/100001/listings/555/files").mock(
        return_value=httpx.Response(200, json={"listing_file_id": 88})
    )
    adapter = _write_adapter(tmp_path)
    ref = adapter.upload_listing_file(555, b"raw-file-bytes", name="art.tif", rank=1)

    assert ref.listing_file_id == 88
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"art.tif" in sent.content
    assert b"raw-file-bytes" in sent.content


@respx.mock
def test_update_listing_patches_fields_only(tmp_path) -> None:
    route = respx.patch(f"{BASE}/shops/100001/listings/555").mock(
        return_value=httpx.Response(
            200,
            json={
                "listing_id": 555,
                "title": "New title",
                "state": "draft",
                "quantity": 999,
                "price": {"amount": 1500, "divisor": 100, "currency_code": "USD"},
            },
        )
    )
    adapter = _write_adapter(tmp_path)
    listing = adapter.update_listing(555, EtsyListingUpdate(title="New title"))

    assert listing.title == "New title"
    sent = route.calls.last.request
    assert sent.headers["content-type"] == "application/x-www-form-urlencoded"
    body = dict(urllib.parse.parse_qsl(sent.content.decode()))
    assert body == {"title": "New title"}  # only the field that was set, no price, no state


@respx.mock
def test_publish_listing_patches_state_active(tmp_path) -> None:
    route = respx.patch(f"{BASE}/shops/100001/listings/555").mock(
        return_value=httpx.Response(
            200,
            json={
                "listing_id": 555,
                "title": "T",
                "state": "active",
                "quantity": 999,
                "price": {"amount": 1500, "divisor": 100, "currency_code": "USD"},
            },
        )
    )
    adapter = _write_adapter(tmp_path)
    listing = adapter.publish_listing(555)

    assert listing.state == "active"
    sent = route.calls.last.request
    assert sent.headers["content-type"] == "application/x-www-form-urlencoded"
    assert sent.content == b"state=active"


@respx.mock
def test_update_listing_price_round_trips_inventory_json(tmp_path) -> None:
    # GET returns price as a Money object {amount,divisor,currency_code} --
    # updateListingInventory's PUT body expects a plain decimal there
    # instead (reviewer finding, M5a slice 4 fix-up).
    get_route = respx.get(f"{BASE}/listings/555/inventory").mock(
        return_value=httpx.Response(
            200,
            json={
                "products": [
                    {
                        "product_id": 1,
                        "sku": "",
                        "offerings": [
                            {
                                "offering_id": 11,
                                "quantity": 999,
                                "is_enabled": True,
                                "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"},
                            }
                        ],
                    }
                ]
            },
        )
    )
    put_route = respx.put(f"{BASE}/listings/555/inventory").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    adapter = _write_adapter(tmp_path)

    adapter.update_listing_price(555, 9.50)

    assert get_route.called
    sent = put_route.calls.last.request
    assert sent.headers["content-type"] == "application/json"
    import json as _json

    body = _json.loads(sent.content)
    offering = body["products"][0]["offerings"][0]
    assert offering["price"] == 9.50  # decimal, not a Money object
    assert offering["offering_id"] == 11  # rest of the structure round-trips verbatim
    assert offering["quantity"] == 999


@respx.mock
def test_update_listing_price_get_failure_raises(tmp_path) -> None:
    respx.get(f"{BASE}/listings/555/inventory").mock(
        return_value=httpx.Response(404, json={"error": "no such listing"})
    )
    adapter = _write_adapter(tmp_path)

    with pytest.raises(EtsyWriteError) as exc_info:
        adapter.update_listing_price(555, 9.50)
    assert exc_info.value.status_code == 404


@respx.mock
def test_update_listing_price_put_failure_raises(tmp_path) -> None:
    respx.get(f"{BASE}/listings/555/inventory").mock(
        return_value=httpx.Response(200, json={"products": []})
    )
    respx.put(f"{BASE}/listings/555/inventory").mock(
        return_value=httpx.Response(400, json={"error": "bad price"})
    )
    adapter = _write_adapter(tmp_path)

    with pytest.raises(EtsyWriteError, match="bad price"):
        adapter.update_listing_price(555, 9.50)


@respx.mock
def test_delete_listing_sends_delete(tmp_path) -> None:
    route = respx.delete(f"{BASE}/shops/100001/listings/555").mock(return_value=httpx.Response(204))
    adapter = _write_adapter(tmp_path)
    adapter.delete_listing(555)
    assert route.called


@respx.mock
def test_write_error_scrubs_body_never_leaks_token(tmp_path) -> None:
    respx.post(f"{BASE}/shops/100001/listings").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": "invalid taxonomy_id",
                "access_token": "super-secret-token-value",
            },
        )
    )
    adapter = _write_adapter(tmp_path)

    with pytest.raises(EtsyWriteError) as exc_info:
        adapter.create_draft_listing(_spec())

    message = str(exc_info.value)
    assert "invalid taxonomy_id" in message
    assert "super-secret-token-value" not in message
    assert exc_info.value.status_code == 400


@respx.mock
def test_write_error_truncates_long_body(tmp_path) -> None:
    respx.post(f"{BASE}/shops/100001/listings").mock(
        return_value=httpx.Response(400, json={"error": "x" * 5000})
    )
    adapter = _write_adapter(tmp_path)

    with pytest.raises(EtsyWriteError) as exc_info:
        adapter.create_draft_listing(_spec())

    assert len(str(exc_info.value)) < 1000
