"""Fixture-backed adapter: the default until live access is approved (PRD §8.4)."""

import json
from pathlib import Path
from typing import Any

from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt, EtsyShop


class FixtureEtsyAdapter:
    def __init__(self, fixture_dir: Path):
        self._dir = Path(fixture_dir)

    def _load(self, name: str) -> dict[str, Any]:
        return json.loads((self._dir / f"{name}.json").read_text())

    def get_shop(self) -> EtsyShop:
        return EtsyShop.model_validate(self._load("shop"))

    def list_listings(self) -> list[EtsyListing]:
        return [EtsyListing.model_validate(r) for r in self._load("listings")["results"]]

    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]:
        receipts = [EtsyReceipt.model_validate(r) for r in self._load("receipts")["results"]]
        if min_created is not None:
            receipts = [r for r in receipts if r.created_timestamp >= min_created]
        return receipts
