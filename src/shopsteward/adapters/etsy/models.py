"""Pydantic models mirroring the Etsy Open API v3 shapes we consume."""

from pydantic import BaseModel, Field


class Money(BaseModel):
    amount: int
    divisor: int
    currency_code: str

    @property
    def as_float(self) -> float:
        return self.amount / self.divisor


class EtsyShop(BaseModel):
    shop_id: int
    shop_name: str
    listing_active_count: int = 0
    transaction_sold_count: int = 0


class EtsyListing(BaseModel):
    listing_id: int
    title: str
    state: str
    quantity: int
    views: int = 0
    num_favorers: int = 0
    price: Money
    tags: list[str] = Field(default_factory=list)
    should_auto_renew: bool = True
    # Additive, optional (M8b listing.seo_edit description slice) -- every
    # already-stored `etsy.listing.observed` payload predates this field and
    # has no `description` key at all; model_validate() on those old rows
    # still succeeds and simply yields None (no migration, no touching
    # immutable stored events).
    description: str | None = None

    @property
    def price_usd(self) -> float:
        return self.price.as_float


class EtsyListingImage(BaseModel):
    """getListingImages response row (listings_r scope, already held -- no
    new scope). Used by the source-photo-match backfill (archive adopt-local)
    to fetch a listing's product image for perceptual hashing."""

    listing_image_id: int
    rank: int = 1
    url_570xN: str
    full_width: int | None = None
    full_height: int | None = None


class EtsyTransaction(BaseModel):
    transaction_id: int
    listing_id: int
    quantity: int
    price: Money


class EtsyReview(BaseModel):
    """getReviewsByShop response row (feedback_r scope, new -- see
    auth.DEFAULT_SCOPES). Etsy's Review object has no dedicated review id
    field; a review is uniquely identified by the (shop_id, listing_id,
    transaction_id) triple (Etsy allows at most one review per
    transaction), so those three fields are kept rather than inventing an
    id. `review` may be an empty string (a rating with no written text)."""

    shop_id: int
    listing_id: int
    transaction_id: int
    buyer_user_id: int | None = None
    rating: int
    review: str = ""
    language: str | None = None
    create_timestamp: int


class EtsyReceipt(BaseModel):
    receipt_id: int
    created_timestamp: int
    grandtotal: Money
    transactions: list[EtsyTransaction] = Field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return self.grandtotal.as_float


# --- write-path models (M5a, PRD §13 decision 41) --------------------------


class EtsyDraftSpec(BaseModel):
    """Input to create_draft_listing (POST .../listings, form-urlencoded --
    Etsy's OpenAPI spec, not JSON). Digital listings only: type="download",
    no shipping profile. `price` is a plain decimal per the real request
    schema (the Money{amount,divisor,currency_code} shape only appears in
    *responses*, e.g. EtsyListing.price). taxonomy_id is doc-derived and
    "verified at fixture-recording" per the design; who_made/when_made are
    confirmed-valid enum members (Etsy OpenAPI spec)."""

    quantity: int
    title: str
    description: str
    price: float
    who_made: str
    when_made: str
    taxonomy_id: int
    type: str = "download"
    is_supply: bool = False
    tags: list[str] = Field(default_factory=list)
    should_auto_renew: bool = True


class EtsyListingRef(BaseModel):
    listing_id: int
    state: str


class EtsyImageRef(BaseModel):
    listing_image_id: int
    rank: int


class EtsyFileRef(BaseModel):
    listing_file_id: int
    rank: int | None = None


class EtsyListingUpdate(BaseModel):
    """Draft-field updates only (PATCH .../listings/{id}, form-urlencoded).
    Etsy's real updateListing schema has no `price` field -- price is only
    set at create_draft_listing time; a later price change is a M5a slice-4
    concern (not modeled here). state is never a field here either (M8b
    slice 4b write-safety invariant) -- so reprice/seo_edit, which send
    this model, can never touch state. State transitions go through
    publish_listing (draft->active, PRD §13 decision 41) or the dedicated
    update_listing_state (live active<->inactive, used only by
    `listing.deactivate`). should_auto_renew IS legitimately carried here (Etsy E1, M8a
    `listing.autorenew_off` capability, listings_w scope, no new scope) --
    it is not a state flip, it only toggles whether Etsy renews an already-
    active listing automatically."""

    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    should_auto_renew: bool | None = None
