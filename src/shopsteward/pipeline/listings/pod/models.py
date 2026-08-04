"""Pydantic v2 boundary models for pod.json (schema shopsteward.pod/1,
design §6) and the pure catalog-selection output (design §5). Mirrors
listings/models.py's ListingConfig alias convention. pod.json is a SEPARATE
config file from listing.json -- pod_config_hash() (pod/config.py) is its
own hash, never folded into listing.json's config_hash(), because doing so
would orphan every existing digital draft_id (design §2).

Printful is dropped entirely (design §0a, review fix-up A): there is only
one provider mode now, so PodProviderCatalog carries no `mode` field, and
PodCatalogVariant carries no `placement` (a Printful-only field, confirmed
dead -- never set, never read).

Orientation participates in matching (design §5 step 1, review fix-up B):
`aspect_of` (catalog.py) returns the photo's orientation alongside its
aspect class, and every catalog variant declares which orientation(s) its
template actually accepts, because a same-ratio landscape and portrait
photo are NOT interchangeable once a `fit_method:"slice"` provider
centre-crops the mismatch."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PodCatalogVariant",
    "PodConfig",
    "PodDropReason",
    "PodDroppedVariant",
    "PodOrientation",
    "PodProductCatalog",
    "PodProviderCatalog",
    "PodRoutingRule",
    "PodVariant",
]

# The photo's own orientation (from proj_landing_files width/height). A
# catalog variant may also declare "any" (its template genuinely accepts
# either) -- see PodCatalogVariant.orientation below.
PodOrientation = Literal["landscape", "portrait", "square"]

# Every reason select_variants() can drop a candidate product type for
# (design §3's dropped[] event payload). Shared alias so the precedence
# constant and the internal resolution functions in catalog.py stop being
# typed as bare str/set[str] (review fix-up F) -- a drifted value would
# otherwise validate silently and persist permanently in an append-only
# event. "above_max_price" is slice 2's (pricing); defined here so the enum
# is stable, but select_variants() never produces it yet.
PodDropReason = Literal["aspect", "dpi", "orientation", "above_max_price", "no_route", "no_variant"]


class _PodPrintFile(BaseModel):
    prefer: str
    max_bytes: int
    min_dpi: int = Field(gt=0)  # an 800px thumbnail must not admit to a 16x20in print
    aspect_tolerance: float = Field(gt=0, le=0.05)  # unbounded lets a panorama misclassify
    host_ttl_seconds: int


class PodRoutingRule(BaseModel):
    product_type: str
    region: str
    providers: list[str]


class PodCatalogVariant(BaseModel):
    format: str
    size: str
    aspect: str
    orientation: PodOrientation | Literal["any"]
    long_edge_inches: float = Field(gt=0)  # a <=0 typo must fail at load, not surface as "dpi"
    variant_key: str
    placeholder: str | None = None
    fit_method: str | None = None
    base_cost: float
    shipping_est: float


class PodProductCatalog(BaseModel):
    template_id: str | None = None
    variants: list[PodCatalogVariant] = Field(default_factory=list)


class PodProviderCatalog(BaseModel):
    store_id_env: str
    products: dict[str, PodProductCatalog] = Field(default_factory=dict)


class _PodPricing(BaseModel):
    markup: float
    price_ending: float
    margin_floor_abs: float
    margin_floor_pct: float
    max_price: float
    shipping_included: bool


class _PodCopy(BaseModel):
    title_suffix: dict[str, str] = Field(default_factory=dict)
    description_block: dict[str, str] = Field(default_factory=dict)


class _PodImages(BaseModel):
    max_ours: int
    hard_cap: int
    trim_provider_images: bool


class PodConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(alias="schema")
    name: str
    enabled: bool
    region: str
    currency: str
    print_file: _PodPrintFile
    aspects: dict[str, float]
    formats_by_aspect: dict[str, list[str]]
    routing: list[PodRoutingRule]
    catalog: dict[str, PodProviderCatalog]
    costs_verified_on: str
    cost_staleness_days: int
    pricing: _PodPricing
    # JSON key stays "copy" (design §6); attribute renamed so it doesn't
    # shadow BaseModel.copy() (ListingConfig precedent).
    copy_: _PodCopy = Field(alias="copy")
    images: _PodImages
    link_timeout_seconds: int
    link_poll_interval_seconds: int


# --- pure selection output (pod/catalog.py, design §5) ----------------------


class PodVariant(BaseModel):
    """One surviving candidate from catalog.select_variants -- pre-pricing.
    Slice 2 turns this + pod/pricing.py into a PodVariantSpec for the
    PodAdapter (adapters/pod/models.py)."""

    product_type: str
    provider: str
    format: str
    variant_key: str
    placeholder: str | None
    fit_method: str | None
    size: str
    aspect: str
    # Carried forward so listingdraft.variants_selected records WHICH orientation
    # was chosen, not just the ratio class. Orientation-blind selection shipped a
    # portrait hero to a landscape SKU; recording it keeps the fix auditable from
    # the event log alone.
    orientation: PodOrientation | Literal["any"]
    dpi: float
    template_id: str | None
    base_cost: float
    shipping_est: float


class PodDroppedVariant(BaseModel):
    """product_type is None only for a photo-wide drop (no candidate product
    type was ever considered -- an aspect miss, or an aspect class with an
    empty/missing formats_by_aspect entry); otherwise it names the dropped
    candidate product type. "above_max_price" is slice 2's (pricing) --
    defined here so the event payload's reason enum (design §3) is stable,
    but select_variants() never produces it. "orientation" (aspect class
    matched, but no same-aspect variant accepts this photo's orientation),
    "no_route" (no routing rule matched this product_type+region) and
    "no_variant" (a rule matched, but no routed provider stocks a
    same-aspect-and-orientation variant) imply different operator repairs,
    so they are reported separately."""

    product_type: str | None
    reason: PodDropReason
