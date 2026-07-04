import httpx
import respx

from shopsteward.adapters.etsy.live import LiveEtsyAdapter

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
