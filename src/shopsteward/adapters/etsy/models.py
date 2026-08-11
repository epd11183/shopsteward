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

    @property
    def price_usd(self) -> float:
        return self.price.as_float


class EtsyTransaction(BaseModel):
    transaction_id: int
    listing_id: int
    quantity: int
    price: Money


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
