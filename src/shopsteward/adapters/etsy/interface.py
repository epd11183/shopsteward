"""Adapter protocol. Core code depends on this, never on an SDK/HTTP client."""

from typing import Protocol

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

_MAX_ERROR_LEN = 500


class EtsyAdapter(Protocol):
    def get_shop(self) -> EtsyShop: ...
    def list_listings(self) -> list[EtsyListing]: ...
    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]: ...


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
    cleanup only."""

    def create_draft_listing(self, spec: EtsyDraftSpec) -> EtsyListingRef: ...
    def upload_listing_image(self, listing_id: int, image: bytes, *, rank: int) -> EtsyImageRef: ...
    def upload_listing_file(
        self, listing_id: int, file: bytes, *, name: str, rank: int
    ) -> EtsyFileRef: ...
    def update_listing(self, listing_id: int, fields: EtsyListingUpdate) -> EtsyListing: ...
    def publish_listing(self, listing_id: int) -> EtsyListing: ...
    def delete_listing(self, listing_id: int) -> None: ...
