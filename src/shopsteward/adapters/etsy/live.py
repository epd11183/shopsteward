"""Live Etsy Open API v3 client. Read-only (get_shop/list_listings/
list_receipts) is wired into `shopsteward sync --live`, triple-gated by
pipeline.live_gate.live_etsy_read_open() (PRD §8.4, M1). LiveEtsyWriteAdapter
below stays separately gated (live_etsy_write_open(), M5a) -- this class has
no write methods at all, so the read path can never reach one."""

import httpx
import pydantic

from shopsteward.adapters.etsy.auth import EtsyTokenAuth, EtsyTokenStore, api_key_header
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.adapters.etsy.models import (
    EtsyDraftSpec,
    EtsyFileRef,
    EtsyImageRef,
    EtsyListing,
    EtsyListingImage,
    EtsyListingRef,
    EtsyListingUpdate,
    EtsyReceipt,
    EtsyShop,
)

BASE = "https://openapi.etsy.com/v3/application"
_LISTING_STATES = ("active", "expired")
# Etsy's image CDN hosts (url_570xN values observed on real listings) --
# download_image() only ever needs to fetch from these, so anything else is
# rejected outright rather than followed (closes an SSRF-shape gap: an
# attacker-influenced API response could otherwise point this at an arbitrary
# internal or third-party host).
_ALLOWED_IMAGE_HOSTS = {"i.etsystatic.com"}


def _encode_form_data(model: pydantic.BaseModel) -> dict[str, str | bool | int | float]:
    """Etsy's form-urlencoded array fields (tags, materials, ...) are a
    SINGLE comma-joined value, not JSON and not a repeated key -- a bare
    list passed to httpx's `data=` serializes as repeated `tags=a&tags=b&...`
    (doseq), and Etsy's parser keeps only the last one, silently truncating
    to one tag. Confirmed live (real PATCH): 10 proposed tags landed as 1.
    An empty list is skipped entirely (never sent as `tags=`) -- Etsy reads
    an explicit empty value as "clear this field", and nothing here means to
    do that; absent is the safe, conservative no-op. NOTE: this means
    `listing.seo_edit`'s undo() can never restore a listing that legitimately
    had zero tags before an edit -- pre-existing behavior, not a regression,
    but worth being explicit about since a silent no-op during a rollback is
    exactly the kind of thing that's confusing during an incident.

    Shared by update_listing() and create_draft_listing() so the two write
    paths can never diverge on this again. This function does NOT itself
    reject a tag containing a literal comma -- that would corrupt the
    comma-join below into extra tags on Etsy's side, undetectably. Every
    caller that can put LLM- or operator-authored tag content in front of
    this encoder MUST validate it first via
    `adapters.copy.tags.validate_tag` (or a Pydantic field_validator that
    calls it, as `CopyVerdict` and `GateEditFields` do) -- the invariant is
    "validated before this function is ever reached", not a fixed list of
    call sites to keep in sync here."""
    raw = model.model_dump(exclude_none=True)
    return {k: ",".join(v) if isinstance(v, list) else v for k, v in raw.items() if v != []}


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

    def get_listing_images(self, listing_id: int) -> list[EtsyListingImage]:
        # A 404 here means the listing has no images available at this
        # endpoint (e.g. an old/expired listing) -- treat that as "no
        # images", same as an empty results list, instead of raising.
        try:
            body = self._get(f"/shops/{self._shop_id}/listings/{listing_id}/images")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise
        return [EtsyListingImage.model_validate(r) for r in body["results"]]

    def download_image(self, url: str) -> bytes:
        # Etsy's image CDN (url_570xN) is a public asset URL -- no auth
        # header needed and none is sent (a fresh, unauthenticated client
        # avoids leaking the shop's bearer token to a third-party host).
        host = httpx.URL(url).host
        if host not in _ALLOWED_IMAGE_HOSTS:
            raise ValueError(f"download_image: host {host!r} is not an allowed Etsy CDN host")
        resp = httpx.get(url, timeout=30.0)
        resp.raise_for_status()
        return resp.content


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
        # the endpoint inherently creates drafts. See _encode_form_data for
        # why list fields (tags) must be comma-joined rather than left as a
        # bare list (httpx's repeated-key doseq encoding truncates them on
        # Etsy's side -- confirmed on the wire, same bug update_listing had).
        body = self._request(
            "POST", f"/shops/{self._shop_id}/listings", data=_encode_form_data(spec)
        )
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
        # updateListing is also form-urlencoded (Etsy OpenAPI spec), not
        # JSON -- see _encode_form_data for the comma-join/empty-list rules.
        body = self._request(
            "PATCH",
            f"/shops/{self._shop_id}/listings/{listing_id}",
            data=_encode_form_data(fields),
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

    def update_listing_state(self, listing_id: int, state: str) -> None:
        # Dedicated method, not a field on EtsyListingUpdate (M8b slice 4b,
        # draft #7 write-safety invariant) -- only listing.deactivate and
        # listing.renew call this. Etsy E4 routes state changes through
        # updateListing itself
        # (form-urlencoded PATCH, same endpoint update_listing/
        # publish_listing use) -- just a different, single field.
        if state not in ("active", "inactive"):
            raise ValueError(f"update_listing_state: unsupported state {state!r}")
        self._request(
            "PATCH", f"/shops/{self._shop_id}/listings/{listing_id}", data={"state": state}
        )

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
