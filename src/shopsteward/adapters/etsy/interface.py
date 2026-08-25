"""Adapter protocol. Core code depends on this, never on an SDK/HTTP client."""

from typing import Protocol

from shopsteward.adapters.etsy.models import (
    EtsyActiveListingsPage,
    EtsyDraftSpec,
    EtsyFileRef,
    EtsyImageRef,
    EtsyListing,
    EtsyListingImage,
    EtsyListingInventory,
    EtsyListingRef,
    EtsyListingUpdate,
    EtsyReceipt,
    EtsyReview,
    EtsyShop,
    EtsyShopSection,
    EtsyTaxonomyNode,
)

_MAX_ERROR_LEN = 500


class EtsyAdapter(Protocol):
    def get_shop(self) -> EtsyShop: ...
    def list_listings(self) -> list[EtsyListing]: ...
    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]: ...
    def list_reviews(self) -> list[EtsyReview]:
        """getReviewsByShop (feedback_r scope, new -- roadmap P4 "review
        velocity"). Raises the adapter's normal HTTP-error behavior on
        failure; a 403 for missing scope is handled by the caller
        (core.sync.sync_etsy), not swallowed here."""
        ...

    def list_shop_sections(self) -> list[EtsyShopSection]:
        """getShopSections (shops_r scope, already held -- no new scope).
        Dead code until a future storefront-organization variant -- no
        current caller anywhere in this codebase."""
        ...

    def list_taxonomy_nodes(self) -> list[EtsyTaxonomyNode]:
        """getSellerTaxonomyNodes -- a global taxonomy tree, not shop-scoped
        (no {shop_id} in the path). Dead code until a future variant -- no
        current caller."""
        ...

    def get_listing_images(self, listing_id: int) -> list[EtsyListingImage]:
        """Returns [] if the listing has no images available (including a
        404 from the images endpoint, e.g. an old/expired listing) -- not
        an error case for callers to special-case."""
        ...

    def download_image(self, url: str) -> bytes:
        """Fetch raw image bytes for a `url_570xN` from get_listing_images.
        Lives on the adapter (not a bare httpx call in core code) so core
        never imports an SDK/HTTP client directly -- same rule that keeps
        every other external call behind this Protocol."""
        ...

    def get_listing_inventory(self, listing_id: int) -> EtsyListingInventory:
        """getListingInventory (GET .../listings/{listing_id}/inventory,
        listings_r scope, already held -- no new scope). Read-only and safe
        for ANY listing, POD or digital -- it never modifies anything.
        Returns an empty EtsyListingInventory (products=[]) for a listing
        with no inventory record (e.g. one never edited via Etsy's
        inventory tools), same "absence is not an error" shape as
        get_listing_images."""
        ...

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
        """findAllListingsActive (GET /v3/application/listings/active) -- a
        global market search across ALL of Etsy, not just this shop, and the
        only method on this Protocol authenticated with x-api-key alone (no
        OAuth access token, no scope at all -- confirmed against the real
        Etsy OAS). This is the free, first-party demand/competition signal
        this shop has never used: no keyword-volume API exists on Etsy at
        all (every paid tool -- eRank/Marmalade/EverBee/Alura -- sells a
        proprietary scrape, not a public API), but `count` (total matching
        listings, a competition proxy) plus the ranked `results` (Etsy's own
        relevance ranker, see below) get most of what those tools sell.

        `sort_on="score"` is Etsy's own relevance ranker and is ALWAYS
        DESCENDING regardless of `sort_order` (a real, confirmed Etsy API
        quirk) -- `keyword_probe.py` relies on that ordering to treat the
        first `limit` rows as "what Etsy's ranker currently rewards" for
        this phrase. `keywords` must appear in every returned listing's
        searchable text (Etsy's own AND-match semantics for this param).
        """
        ...


class EtsyWriteError(RuntimeError):
    """Raised by EtsyWriteAdapter implementations on any write failure.
    Carries only the HTTP status and Etsy's `error` field -- never the raw
    response body (upload-error bodies can echo file contents, and no body
    is ever assumed safe to print); the message is truncated defensively."""

    def __init__(self, status_code: int, error: str | None):
        self.status_code = status_code
        self.error = error
        message = f"Etsy write failed with HTTP {status_code}"
        if error:
            message += f": {error[:_MAX_ERROR_LEN]}"
        super().__init__(message)


class EtsyWriteAdapter(Protocol):
    """Create/update DRAFT Etsy listings only (PRD §13 decision 41).
    publish_listing is implemented at the adapter level, but the sole caller
    anywhere in this codebase is the Gate 3 endpoint (M5a slice 4) -- the
    push stage (slice 3) never calls it. delete_listing is smoke-test
    cleanup only. update_listing_price is separate from update_listing
    because Etsy's real updateListing has no price field -- a price change
    goes through updateListingInventory instead (reviewer finding, M5a
    slice 4 fix-up). update_listing_state is likewise a DEDICATED method,
    not a `state` field on EtsyListingUpdate (M8b slice 4b, draft #7,
    write-safety invariant): only `listing.deactivate` and `listing.renew`
    ever call it, so SEO edit / reprice -- both of which use
    EtsyListingUpdate -- can never touch state. Accepts only
    "active"/"inactive"."""

    def create_shop_section(self, title: str) -> EtsyShopSection:
        """createShopSection (shops_r + shops_w scopes, already held -- no
        new scope). Dead code until a future storefront-organization
        variant -- no current caller anywhere in this codebase."""
        ...

    def create_draft_listing(self, spec: EtsyDraftSpec) -> EtsyListingRef: ...
    def upload_listing_image(self, listing_id: int, image: bytes, *, rank: int) -> EtsyImageRef: ...
    def upload_listing_file(
        self, listing_id: int, file: bytes, *, name: str, rank: int
    ) -> EtsyFileRef: ...
    def update_listing(self, listing_id: int, fields: EtsyListingUpdate) -> EtsyListing: ...
    def update_listing_price(self, listing_id: int, price: float) -> None: ...
    def update_listing_state(self, listing_id: int, state: str) -> None: ...

    def update_listing_inventory(
        self, listing_id: int, inventory: EtsyListingInventory
    ) -> EtsyListingInventory:
        """updateListingInventory (PUT .../listings/{listing_id}/inventory).
        Sets a listing's ENTIRE products/offerings/property_values array --
        i.e. its full SKU and variation structure, not just price.

        *** WARNING -- READ BEFORE CALLING, THIS METHOD HAS NO GUARD. ***
        CLAUDE.md's "POD-first listing creation for physical SKUs" rule
        states, verbatim: "Gelato/Printful APIs create the product and push
        the Etsy draft; we then enrich the draft (title, tags, description,
        images, price) via the Etsy API. Never modify provider-set SKU
        values or variation structure." Calling this method against a
        POD-backed listing (one Gelato/Printful created) can rewrite exactly
        the SKU/variation structure that rule forbids touching.

        This adapter method deliberately does NOT check whether a listing is
        POD or digital -- that is a business-logic judgment call the adapter
        layer has no way to make on its own, matching how every other write
        primitive here (update_listing, update_listing_price, ...) is
        unopinionated and leaves eligibility to the capability that calls
        it. ANY future capability that calls update_listing_inventory MUST
        first apply the same digital-only eligibility check
        `listing.reprice` uses before ever calling updateListingInventory:
        `_is_conservatively_digital()` in
        `shopsteward/pipeline/ops/capabilities/reprice.py`, PLUS reprice's
        authoritative `listingdraft.provider_linked` event-log check (a
        listing_id that ever appears there is POD-backed, full stop,
        regardless of title -- note this is authoritative only for a
        listing this app itself created via the POD flow and logged;
        a POD listing created outside ShopSteward has no such event, so
        the title heuristic is still the only signal for that case).

        Be explicit with yourself about what this docstring is and is not:
        it is a label on a loaded gun, not a safety catch. As of this
        writing NO capability in this codebase calls
        update_listing_inventory at all, so there is nothing here or
        upstream enforcing the digital-only check yet -- do not mistake
        this warning for an enforced guard."""
        ...

    def publish_listing(self, listing_id: int) -> EtsyListing: ...
    def delete_listing(self, listing_id: int) -> None: ...
