"""Live Etsy Open API v3 client. Read-only (get_shop/list_listings/
list_receipts) is wired into `shopsteward sync --live`, triple-gated by
pipeline.live_gate.live_etsy_read_open() (PRD §8.4, M1). LiveEtsyWriteAdapter
below stays separately gated (live_etsy_write_open(), M5a) -- this class has
no write methods at all, so the read path can never reach one."""

import httpx

from shopsteward.adapters.etsy.auth import EtsyTokenAuth, EtsyTokenStore, api_key_header
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.adapters.etsy.models import (
    EtsyDraftSpec,
    EtsyFileRef,
    EtsyImageRef,
    EtsyListing,
    EtsyListingRef,
    EtsyListingUpdate,
    EtsyReceipt,
    EtsyShop,
)

BASE = "https://openapi.etsy.com/v3/application"
_LISTING_STATES = ("active", "expired")


def _safe_error(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        error = body.get("error")
        return error if isinstance(error, str) else None
    return None


class LiveEtsyAdapter:
    def __init__(self, api_key: str, shop_id: int, access_token: str):
        self._shop_id = shop_id
        self._client = httpx.Client(
            headers={
                "x-api-key": api_key_header(api_key),
                "authorization": f"Bearer {access_token}",
            },
            timeout=30.0,
        )

    def _get(self, path: str, **params: int | str) -> dict:
        resp = self._client.get(f"{BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, **params: int | str) -> list[dict]:
        results: list[dict] = []
        offset = 0
        while True:
            page = self._get(path, limit=100, offset=offset, **params)
            results.extend(page["results"])
            offset += 100
            if offset >= page["count"]:
                return results

    def get_shop(self) -> EtsyShop:
        return EtsyShop.model_validate(self._get(f"/shops/{self._shop_id}"))

    def list_listings(self) -> list[EtsyListing]:
        # getListingsByShop (not findAllListingsActiveByShop) so expired
        # listings aren't invisible to a "what's dying" analysis -- same
        # listings_r scope, just a `state` filter on the general listings
        # endpoint instead of the active-only one. draft/sold_out are not
        # fetched here; add states to _LISTING_STATES if that's ever needed.
        rows: list[dict] = []
        for state in _LISTING_STATES:
            rows.extend(self._paginate(f"/shops/{self._shop_id}/listings", state=state))
        return [EtsyListing.model_validate(r) for r in rows]

    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]:
        params: dict[str, int] = {"min_created": min_created} if min_created is not None else {}
        rows = self._paginate(f"/shops/{self._shop_id}/receipts", **params)
        return [EtsyReceipt.model_validate(r) for r in rows]


class LiveEtsyWriteAdapter:
    """Live Etsy Open API v3 write client (PRD §13 decision 41). A separate
    class from LiveEtsyAdapter -- the read path (M1) and its fixtures stay
    untouched, and the write path is wired to EtsyTokenAuth (auto-refreshing)
    rather than a bare access token. NOT wired into any default path; live
    use is triple-gated by pipeline.live_gate.live_etsy_write_open()."""

    def __init__(self, api_key: str, shop_id: int, token_store: EtsyTokenStore):
        self._shop_id = shop_id
        self._client = httpx.Client(
            auth=EtsyTokenAuth(token_store, api_key),
            headers={"x-api-key": api_key_header(api_key)},
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs: object) -> dict:
        resp = self._client.request(method, f"{BASE}{path}", **kwargs)
        if resp.status_code >= 400:
            raise EtsyWriteError(resp.status_code, _safe_error(resp))
        return resp.json()

    def create_draft_listing(self, spec: EtsyDraftSpec) -> EtsyListingRef:
        # createDraftListing is application/x-www-form-urlencoded, not JSON
        # (Etsy OpenAPI spec) -- there is no `state` request field at all,
        # the endpoint inherently creates drafts. httpx encodes list values
        # (tags) as repeated keys and bools as lowercase "true"/"false".
        body = self._request("POST", f"/shops/{self._shop_id}/listings", data=spec.model_dump())
        ref = EtsyListingRef(listing_id=body["listing_id"], state=body.get("state", "draft"))
        if ref.state != "draft":
            # Never trust that blindly -- write-safety invariant (PRD §13
            # decision 41).
            raise EtsyWriteError(
                500, f"createDraftListing returned state={ref.state!r}, expected draft"
            )
        return ref

    def upload_listing_image(self, listing_id: int, image: bytes, *, rank: int) -> EtsyImageRef:
        body = self._request(
            "POST",
            f"/shops/{self._shop_id}/listings/{listing_id}/images",
            data={"rank": rank},
            files={"image": ("image.jpg", image, "image/jpeg")},
        )
        return EtsyImageRef(listing_image_id=body["listing_image_id"], rank=body.get("rank", rank))

    def upload_listing_file(
        self, listing_id: int, file: bytes, *, name: str, rank: int
    ) -> EtsyFileRef:
        body = self._request(
            "POST",
            f"/shops/{self._shop_id}/listings/{listing_id}/files",
            data={"name": name, "rank": rank},
            files={"file": (name, file, "application/octet-stream")},
        )
        return EtsyFileRef(listing_file_id=body["listing_file_id"], rank=body.get("rank"))

    def update_listing(self, listing_id: int, fields: EtsyListingUpdate) -> EtsyListing:
        # updateListing is also form-urlencoded (Etsy OpenAPI spec), not JSON.
        body = self._request(
            "PATCH",
            f"/shops/{self._shop_id}/listings/{listing_id}",
            data=fields.model_dump(exclude_none=True),
        )
        return EtsyListing.model_validate(body)

    def update_listing_price(self, listing_id: int, price: float) -> None:
        # Etsy's real updateListing has no price field -- a post-create price
        # change goes through updateListingInventory instead (JSON, not
        # form-urlencoded). The GET response carries read-only keys the PUT
        # rejects with HTTP 400 "Array contains invalid keys" (product_id,
        # is_deleted, offering_id, Money-shaped price...), verified against
        # the live API in the §8.4 write smoke -- so this WHITELISTS the
        # writable fields instead of round-tripping verbatim.
        inventory = self._request("GET", f"/listings/{listing_id}/inventory")
        products = []
        for product in inventory.get("products", []):
            offerings = [
                {
                    "price": price,
                    "quantity": o.get("quantity"),
                    "is_enabled": o.get("is_enabled", True),
                }
                for o in product.get("offerings", [])
            ]
            out = {
                "property_values": [
                    {
                        k: v
                        for k, v in pv.items()
                        if k in ("property_id", "value_ids", "scale_id", "property_name", "values")
                        and v is not None
                    }
                    for pv in product.get("property_values", [])
                ],
                "offerings": offerings,
            }
            if product.get("sku"):
                out["sku"] = product["sku"]
            products.append(out)
        self._request("PUT", f"/listings/{listing_id}/inventory", json={"products": products})

    def publish_listing(self, listing_id: int) -> EtsyListing:
        # The sole caller anywhere in this codebase is the Gate 3 endpoint
        # (M5a slice 4, PRD §13 decision 41). state is the only field this
        # endpoint accepts for publishing (enum: active|inactive).
        body = self._request(
            "PATCH", f"/shops/{self._shop_id}/listings/{listing_id}", data={"state": "active"}
        )
        return EtsyListing.model_validate(body)

    def delete_listing(self, listing_id: int) -> None:
        resp = self._client.delete(f"{BASE}/shops/{self._shop_id}/listings/{listing_id}")
        if resp.status_code >= 400:
            raise EtsyWriteError(resp.status_code, _safe_error(resp))
