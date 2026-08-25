"""Fixture-backed adapter: the default until live access is approved (PRD §8.4)."""

import json
from pathlib import Path
from typing import Any

from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.adapters.etsy.models import (
    EtsyActiveListingsPage,
    EtsyDraftSpec,
    EtsyFileRef,
    EtsyImageRef,
    EtsyListing,
    EtsyListingImage,
    EtsyListingInventory,
    EtsyListingProduct,
    EtsyListingRef,
    EtsyListingUpdate,
    EtsyReceipt,
    EtsyReview,
    EtsyShop,
    EtsyShopSection,
    EtsyTaxonomyNode,
    Money,
)


class FixtureEtsyAdapter:
    def __init__(self, fixture_dir: Path):
        self._dir = Path(fixture_dir)

    def _load(self, name: str) -> dict[str, Any]:
        # encoding="utf-8" explicit (M4, guardrail review 2026-08-25) --
        # these are committed Etsy fixtures, and titles routinely carry
        # em dashes/accents; `read_text()`'s platform-default encoding is
        # cp1252 on Windows, which silently mojibake-corrupts them.
        return json.loads((self._dir / f"{name}.json").read_text(encoding="utf-8"))

    def get_shop(self) -> EtsyShop:
        return EtsyShop.model_validate(self._load("shop"))

    def list_listings(self) -> list[EtsyListing]:
        return [EtsyListing.model_validate(r) for r in self._load("listings")["results"]]

    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]:
        receipts = [EtsyReceipt.model_validate(r) for r in self._load("receipts")["results"]]
        if min_created is not None:
            receipts = [r for r in receipts if r.created_timestamp >= min_created]
        return receipts

    def list_reviews(self) -> list[EtsyReview]:
        return [EtsyReview.model_validate(r) for r in self._load("reviews")["results"]]

    def list_shop_sections(self) -> list[EtsyShopSection]:
        return [EtsyShopSection.model_validate(r) for r in self._load("shop_sections")["results"]]

    def list_taxonomy_nodes(self) -> list[EtsyTaxonomyNode]:
        return [EtsyTaxonomyNode.model_validate(r) for r in self._load("taxonomy_nodes")["results"]]

    def get_listing_images(self, listing_id: int) -> list[EtsyListingImage]:
        # listing_images.json maps listing_id (string key, JSON has no int
        # keys) -> list of image rows. `url_570xN` here is a filename
        # relative to the fixture dir, not a real CDN URL -- this is a fake,
        # download_image below resolves it the same way.
        rows = self._load("listing_images").get(str(listing_id), [])
        return [EtsyListingImage.model_validate(r) for r in rows]

    def download_image(self, url: str) -> bytes:
        return (self._dir / url).read_bytes()

    def get_listing_inventory(self, listing_id: int) -> EtsyListingInventory:
        # listing_inventory.json maps listing_id (string key) -> a full
        # getListingInventory response body, same "no fixture row = empty
        # result" shape get_listing_images uses for an unknown listing_id.
        row = self._load("listing_inventory").get(str(listing_id))
        return EtsyListingInventory.model_validate(row) if row else EtsyListingInventory()

    def find_active_listings(
        self,
        keywords: str,
        *,
        taxonomy_id: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        limit: int = 25,
        sort_on: str = "score",
    ) -> EtsyActiveListingsPage:
        # active_listings.json maps a keyword phrase -> a full
        # findAllListingsActive response body (already sorted the way a
        # sort_on="score" request would return it). taxonomy_id/min_price/
        # max_price/sort_on are NOT applied to fixture data -- each fixture
        # entry represents one canned query's result already; `limit`
        # truncates it, same shape a real Etsy `limit` param would.
        body = self._load("active_listings").get(keywords)
        if body is None:
            return EtsyActiveListingsPage(count=0, results=[])
        page = EtsyActiveListingsPage.model_validate(body)
        return EtsyActiveListingsPage(count=page.count, results=page.results[:limit])


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
        self._next_section_id = 1
        self._next_product_id = 1
        self._next_offering_id = 1
        self.listings: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _require(self, listing_id: int) -> dict[str, Any]:
        row = self.listings.get(listing_id)
        if row is None:
            raise EtsyWriteError(404, f"unknown Etsy listing_id {listing_id}")
        return row

    def create_shop_section(self, title: str) -> EtsyShopSection:
        section_id = self._next_section_id
        self._next_section_id += 1
        section = EtsyShopSection(
            shop_section_id=section_id, title=title, rank=section_id, user_id=1
        )
        self.calls.append(("create_shop_section", {"section_id": section_id, "title": title}))
        return section

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

    def update_listing_inventory(
        self, listing_id: int, inventory: EtsyListingInventory
    ) -> EtsyListingInventory:
        # *** WARNING -- see interface.EtsyWriteAdapter.update_listing_inventory's
        # docstring before calling this from any capability: no digital-only
        # guard exists at this layer (CLAUDE.md POD-first rule). *** Mirrors
        # the real PUT response -- assigns fresh product_id/offering_id to
        # each row (the live API's response-only identifiers) and stores the
        # result so a later get_listing_inventory-shaped read (if ever
        # added to this fake) would see it; there is no separate read fake
        # method here since EtsyWriteAdapter has no such read.
        row = self._require(listing_id)
        stored: list[EtsyListingProduct] = []
        for product in inventory.products:
            offerings = []
            for offering in product.offerings:
                offering_id = self._next_offering_id
                self._next_offering_id += 1
                offerings.append(offering.model_copy(update={"offering_id": offering_id}))
            product_id = self._next_product_id
            self._next_product_id += 1
            stored.append(
                product.model_copy(update={"product_id": product_id, "offerings": offerings})
            )
        result = EtsyListingInventory(
            products=stored,
            price_on_property=inventory.price_on_property,
            quantity_on_property=inventory.quantity_on_property,
            sku_on_property=inventory.sku_on_property,
            readiness_state_on_property=inventory.readiness_state_on_property,
        )
        row["inventory"] = result
        self.calls.append(
            ("update_listing_inventory", {"listing_id": listing_id, "inventory": inventory})
        )
        return result

    def update_listing_state(self, listing_id: int, state: str) -> None:
        # Dedicated method, not a field on EtsyListingUpdate (M8b slice 4b,
        # draft #7 write-safety invariant) -- only listing.deactivate and
        # listing.renew call this. Mirrors update_listing_price's
        # separate-call shape.
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
