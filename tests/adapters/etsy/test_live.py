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


def _listing_row(listing_id: int, state: str) -> dict:
    return {
        "listing_id": listing_id,
        "title": "T",
        "state": state,
        "quantity": 1,
        "price": {"amount": 100, "divisor": 100, "currency_code": "USD"},
    }


@respx.mock
def test_list_listings_fetches_active_and_expired_and_parses() -> None:
    # getListingsByShop (not findAllListingsActiveByShop) so expired
    # listings -- invisible to the old active-only endpoint -- come back too.
    respx.get(f"{BASE}/shops/100001/listings", params={"state": "active"}).mock(
        return_value=httpx.Response(
            200, json={"count": 1, "results": [_listing_row(111, "active")]}
        )
    )
    respx.get(f"{BASE}/shops/100001/listings", params={"state": "expired"}).mock(
        return_value=httpx.Response(
            200, json={"count": 1, "results": [_listing_row(222, "expired")]}
        )
    )
    respx.get(f"{BASE}/shops/100001/listings", params={"state": "sold_out"}).mock(
        return_value=httpx.Response(
            200, json={"count": 1, "results": [_listing_row(333, "sold_out")]}
        )
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    listings = adapter.list_listings()
    assert {listing.listing_id for listing in listings} == {111, 222, 333}
    assert {listing.state for listing in listings} == {"active", "expired", "sold_out"}
    sent = respx.calls.last.request
    assert sent.headers["x-api-key"] == "k"
    assert sent.headers["authorization"] == "Bearer tok"


@respx.mock
def test_pagination_follows_count() -> None:
    def pager(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request.url.params)["offset"])
        state = dict(request.url.params)["state"]
        row = _listing_row(offset + 1, state)
        return httpx.Response(200, json={"count": 150, "results": [row]})

    respx.get(f"{BASE}/shops/100001/listings").mock(side_effect=pager)
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    # count=150 -> offsets 0 and 100, times 3 states (active, expired, sold_out)
    assert len(adapter.list_listings()) == 6


@respx.mock
def test_get_listing_images_parses_results() -> None:
    respx.get(f"{BASE}/listings/555/images").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "results": [
                    {
                        "listing_image_id": 9001,
                        "rank": 1,
                        "url_570xN": "https://cdn.example/img.jpg",
                        "full_width": 570,
                        "full_height": 570,
                    }
                ],
            },
        )
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    images = adapter.get_listing_images(555)
    assert len(images) == 1
    assert images[0].listing_image_id == 9001
    assert images[0].url_570xN == "https://cdn.example/img.jpg"


@respx.mock
def test_get_listing_images_returns_empty_on_404() -> None:
    # An old/expired listing can 404 at this endpoint -- treat that the
    # same as "no images", not an error (plan_matches already handles the
    # no-images case).
    respx.get(f"{BASE}/listings/555/images").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    assert adapter.get_listing_images(555) == []


@respx.mock
def test_get_listing_images_raises_on_non_404_error() -> None:
    respx.get(f"{BASE}/listings/555/images").mock(
        return_value=httpx.Response(500, json={"error": "server error"})
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    with pytest.raises(httpx.HTTPStatusError):
        adapter.get_listing_images(555)


@respx.mock
def test_get_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # sync_etsy()'s N+1 per-listing image fetch trips Etsy's per-second rate
    # limit on shops with more than a handful of listings -- confirmed live.
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    respx.get(f"{BASE}/listings/555/images").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    assert adapter.get_listing_images(555) == []
    assert slept == [1.0]


@respx.mock
def test_get_raises_after_exhausting_429_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    respx.get(f"{BASE}/listings/555/images").mock(return_value=httpx.Response(429))
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    with pytest.raises(httpx.HTTPStatusError):
        adapter.get_listing_images(555)


@respx.mock
def test_download_image_sends_no_auth_header() -> None:
    route = respx.get("https://i.etsystatic.com/img.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8jpegbytes")
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    data = adapter.download_image("https://i.etsystatic.com/img.jpg")
    assert data == b"\xff\xd8jpegbytes"
    sent = route.calls.last.request
    assert "authorization" not in sent.headers
    assert "x-api-key" not in sent.headers


@respx.mock
def test_download_image_rejects_disallowed_host() -> None:
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    with pytest.raises(ValueError, match="not an allowed"):
        adapter.download_image("https://evil.example/img.jpg")


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
    # tags is a single comma-joined value, not a repeated key or a JSON array
    # (Etsy's array fields are comma-joined form values -- see live.py).
    tags = [v for k, v in urllib.parse.parse_qsl(sent.content.decode()) if k == "tags"]
    assert tags == ["wall art,coastal"]
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
def test_update_listing_comma_joins_list_fields(tmp_path) -> None:
    # Regression: httpx's form-urlencoded `data=` serializes a bare list as
    # REPEATED keys (tags=a&tags=b&tags=c), and Etsy's parser keeps only the
    # last one -- a 10-tag list silently landed as 1 tag on a real listing.
    # Etsy's array fields are a single comma-joined string, not repeated
    # keys and not JSON. A 3-element list catches this; a 1-element list
    # would pass either way.
    route = respx.patch(f"{BASE}/shops/100001/listings/555").mock(
        return_value=httpx.Response(
            200,
            json={
                "listing_id": 555,
                "title": "T",
                "state": "draft",
                "quantity": 999,
                "price": {"amount": 1500, "divisor": 100, "currency_code": "USD"},
            },
        )
    )
    adapter = _write_adapter(tmp_path)
    adapter.update_listing(555, EtsyListingUpdate(title="New title", tags=["a", "b", "c"]))

    sent = route.calls.last.request
    pairs = urllib.parse.parse_qsl(sent.content.decode())
    body = dict(pairs)
    # a single "tags" key with the comma-joined value -- not three repeated
    # "tags" pairs, and not a JSON-encoded list.
    assert [v for k, v in pairs if k == "tags"] == ["a,b,c"]
    assert body["tags"] == "a,b,c"
    # non-list fields must pass through unchanged, not comma-split or mangled.
    assert body["title"] == "New title"


@respx.mock
def test_update_listing_omits_empty_list_fields(tmp_path) -> None:
    # An empty list must be treated as "no-op" (field absent), never as an
    # explicit "clear all tags" instruction -- ",".join([]) == "" would send
    # tags= on the wire, which Etsy reads as "clear the tags".
    route = respx.patch(f"{BASE}/shops/100001/listings/555").mock(
        return_value=httpx.Response(
            200,
            json={
                "listing_id": 555,
                "title": "T",
                "state": "draft",
                "quantity": 999,
                "price": {"amount": 1500, "divisor": 100, "currency_code": "USD"},
            },
        )
    )
    adapter = _write_adapter(tmp_path)
    adapter.update_listing(555, EtsyListingUpdate(title="New title", tags=[]))

    sent = route.calls.last.request
    # keep_blank_values=True -- the default parse_qsl silently drops a
    # `tags=` (empty-value) pair, which would mask this exact bug.
    body = dict(urllib.parse.parse_qsl(sent.content.decode(), keep_blank_values=True))
    assert "tags" not in body
    assert body == {"title": "New title"}


@respx.mock
def test_create_draft_listing_comma_joins_list_fields(tmp_path) -> None:
    # Same repeated-key truncation bug as update_listing's tags field, on the
    # OTHER write path -- confirmed on the wire:
    # ...&tags=a&tags=b&tags=c... truncates to the last value server-side.
    route = respx.post(f"{BASE}/shops/100001/listings").mock(
        return_value=httpx.Response(200, json={"listing_id": 555, "state": "draft"})
    )
    adapter = _write_adapter(tmp_path)
    adapter.create_draft_listing(_spec(tags=["a", "b", "c"]))

    sent = route.calls.last.request
    pairs = urllib.parse.parse_qsl(sent.content.decode())
    assert [v for k, v in pairs if k == "tags"] == ["a,b,c"]


@respx.mock
def test_create_draft_listing_omits_empty_list_fields(tmp_path) -> None:
    route = respx.post(f"{BASE}/shops/100001/listings").mock(
        return_value=httpx.Response(200, json={"listing_id": 555, "state": "draft"})
    )
    adapter = _write_adapter(tmp_path)
    adapter.create_draft_listing(_spec(tags=[]))

    sent = route.calls.last.request
    body = dict(urllib.parse.parse_qsl(sent.content.decode(), keep_blank_values=True))
    assert "tags" not in body


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
def test_update_listing_price_whitelists_inventory_put_body(tmp_path) -> None:
    # GET returns price as a Money object plus read-only keys (product_id,
    # is_deleted, offering_id) that the real PUT rejects with HTTP 400
    # "Array contains invalid keys" -- verified live in the §8.4 write
    # smoke. The PUT body must whitelist writable fields only.
    get_route = respx.get(f"{BASE}/listings/555/inventory").mock(
        return_value=httpx.Response(
            200,
            json={
                "products": [
                    {
                        "product_id": 1,
                        "is_deleted": False,
                        "sku": "",
                        "property_values": [],
                        "offerings": [
                            {
                                "offering_id": 11,
                                "is_deleted": False,
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
    product = body["products"][0]
    offering = product["offerings"][0]
    assert offering["price"] == 9.50  # decimal, not a Money object
    assert offering["quantity"] == 999
    assert offering["is_enabled"] is True
    # read-only keys the real API rejects must be stripped
    assert "product_id" not in product
    assert "is_deleted" not in product
    assert "offering_id" not in offering
    assert "is_deleted" not in offering
    assert "sku" not in product  # empty sku omitted entirely


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
