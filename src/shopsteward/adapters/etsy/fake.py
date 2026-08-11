"""Fixture-backed adapter: the default until live access is approved (PRD §8.4)."""

import json
from pathlib import Path
from typing import Any

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
    Money,
)


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


class FakeEtsyWriteAdapter:
    """In-memory EtsyWriteAdapter twin -- the default everywhere (tests +
    the offline `listings build`/`listings push` default). Enforces the same
    write-safety invariants the live adapter enforces (PRD §13 decision 41):
    every create starts state=draft; publish requires >=1 image and >=1
    digital file already attached; any call against an unknown listing_id
    raises EtsyWriteError(404) instead of silently upserting.
    `calls` records every method invocation (name, kwargs) for assertions.
    """

    def __init__(self) -> None:
        self._next_listing_id = 1000
        self._next_image_id = 1
        self._next_file_id = 1
        self.listings: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _require(self, listing_id: int) -> dict[str, Any]:
        row = self.listings.get(listing_id)
        if row is None:
            raise EtsyWriteError(404, f"unknown Etsy listing_id {listing_id}")
        return row

    def create_draft_listing(self, spec: EtsyDraftSpec) -> EtsyListingRef:
        listing_id = self._next_listing_id
        self._next_listing_id += 1
        self.listings[listing_id] = {
            "title": spec.title,
            "description": spec.description,
            "price": spec.price,
            "quantity": spec.quantity,
            "tags": list(spec.tags),
            "state": "draft",
            "images": [],
            "files": [],
            "should_auto_renew": spec.should_auto_renew,
        }
        self.calls.append(("create_draft_listing", {"listing_id": listing_id, "spec": spec}))
        return EtsyListingRef(listing_id=listing_id, state="draft")

    def seed_listing(
        self,
        listing_id: int,
        *,
        should_auto_renew: bool = True,
        state: str = "active",
        title: str = "seeded listing",
        price: float = 20.0,
        quantity: int = 1,
        tags: list[str] | None = None,
    ) -> None:
        """Test-only preload of a listing the fake didn't create itself (e.g.
        one M8a autonomy tests need `execute()`/`undo()` to be able to
        `update_listing` against). Not part of EtsyWriteAdapter; not
        recorded in `calls` -- it stands in for "Etsy already has this
        listing", not an adapter call this code path made."""
        self.listings[listing_id] = {
            "title": title,
            "description": "",
            "price": price,
            "quantity": quantity,
            "tags": list(tags or []),
            "state": state,
            "images": [],
            "files": [],
            "should_auto_renew": should_auto_renew,
        }

    def upload_listing_image(self, listing_id: int, image: bytes, *, rank: int) -> EtsyImageRef:
        row = self._require(listing_id)
        image_id = self._next_image_id
        self._next_image_id += 1
        row["images"].append({"listing_image_id": image_id, "rank": rank})
        self.calls.append(
            ("upload_listing_image", {"listing_id": listing_id, "rank": rank, "size": len(image)})
        )
        return EtsyImageRef(listing_image_id=image_id, rank=rank)

    def upload_listing_file(
        self, listing_id: int, file: bytes, *, name: str, rank: int
    ) -> EtsyFileRef:
        row = self._require(listing_id)
        file_id = self._next_file_id
        self._next_file_id += 1
        row["files"].append({"listing_file_id": file_id, "rank": rank, "name": name})
        self.calls.append(
            (
                "upload_listing_file",
                {"listing_id": listing_id, "name": name, "rank": rank, "size": len(file)},
            )
        )
        return EtsyFileRef(listing_file_id=file_id, rank=rank)

    def update_listing(self, listing_id: int, fields: EtsyListingUpdate) -> EtsyListing:
        # Etsy's real updateListing has no price field -- price is fixed at
        # create_draft_listing time (EtsyListingUpdate doesn't carry one).
        row = self._require(listing_id)
        updates = fields.model_dump(exclude_none=True)
        row.update(
            {
                k: v
                for k, v in updates.items()
                if k in ("title", "description", "tags", "should_auto_renew")
            }
        )
        self.calls.append(("update_listing", {"listing_id": listing_id, "fields": updates}))
        return self._to_listing(listing_id, row)

    def update_listing_price(self, listing_id: int, price: float) -> None:
        # Mirrors the live adapter's updateListingInventory round-trip
        # (PRD §13 decision 39/41): a post-create price change is a separate
        # call from update_listing.
        row = self._require(listing_id)
        row["price"] = price
        self.calls.append(("update_listing_price", {"listing_id": listing_id, "price": price}))

    def update_listing_state(self, listing_id: int, state: str) -> None:
        # Dedicated method, not a field on EtsyListingUpdate (M8b slice 4b,
        # draft #7 write-safety invariant) -- only listing.deactivate calls
        # this. Mirrors update_listing_price's separate-call shape.
        if state not in ("active", "inactive"):
            raise ValueError(f"update_listing_state: unsupported state {state!r}")
        row = self._require(listing_id)
        row["state"] = state
        self.calls.append(("update_listing_state", {"listing_id": listing_id, "state": state}))

    def publish_listing(self, listing_id: int) -> EtsyListing:
        row = self._require(listing_id)
        if not row["images"]:
            raise EtsyWriteError(400, "cannot publish a listing with zero images")
        if not row["files"]:
            raise EtsyWriteError(400, "cannot publish a listing with zero digital files")
        row["state"] = "active"
        self.calls.append(("publish_listing", {"listing_id": listing_id}))
        return self._to_listing(listing_id, row)

    def delete_listing(self, listing_id: int) -> None:
        self._require(listing_id)
        del self.listings[listing_id]
        self.calls.append(("delete_listing", {"listing_id": listing_id}))

    def _to_listing(self, listing_id: int, row: dict[str, Any]) -> EtsyListing:
        # EtsyListing.price is the Money{amount,divisor,currency_code}
        # *response* shape (Etsy OpenAPI spec) even though the create
        # *request* takes a plain float -- the fake stands in for both ends,
        # so it converts here. Currency isn't part of EtsyDraftSpec (Etsy
        # infers it from the shop), so this defaults to USD like the shipped
        # listing.json config.
        return EtsyListing(
            listing_id=listing_id,
            title=row["title"],
            state=row["state"],
            quantity=row["quantity"],
            price=Money(amount=round(row["price"] * 100), divisor=100, currency_code="USD"),
            tags=row["tags"],
            should_auto_renew=row.get("should_auto_renew", True),
        )
