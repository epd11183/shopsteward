"""Adapter protocol. Core code depends on this, never on an SDK/HTTP client."""

from typing import Protocol

from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt, EtsyShop


class EtsyAdapter(Protocol):
    def get_shop(self) -> EtsyShop: ...
    def list_listings(self) -> list[EtsyListing]: ...
    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]: ...
