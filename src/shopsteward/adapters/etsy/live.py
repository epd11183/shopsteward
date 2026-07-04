"""Live Etsy Open API v3 client. NOT wired into any default path — the CLI
refuses live sync until the operator approves the smoke test (PRD §8.4)."""

import httpx

from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt, EtsyShop

BASE = "https://openapi.etsy.com/v3/application"


class LiveEtsyAdapter:
    def __init__(self, api_key: str, shop_id: int, access_token: str):
        self._shop_id = shop_id
        self._client = httpx.Client(
            headers={"x-api-key": api_key, "authorization": f"Bearer {access_token}"},
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
        rows = self._paginate(f"/shops/{self._shop_id}/listings/active")
        return [EtsyListing.model_validate(r) for r in rows]

    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]:
        params: dict[str, int] = {"min_created": min_created} if min_created else {}
        rows = self._paginate(f"/shops/{self._shop_id}/receipts", **params)
        return [EtsyReceipt.model_validate(r) for r in rows]
