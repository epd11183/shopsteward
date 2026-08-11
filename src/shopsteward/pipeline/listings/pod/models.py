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
centre-crops the mismatch.

Slice 2 (PRICING DECISION 2026-08-04, design §5): `PodCatalogVariant.
retail_override` lets the operator pin a variant's retail price directly,
bypassing the cost*markup solve entirely -- `enforce_floor` (pod/pricing.py)
still asserts an override clears both margin floors, so a below-floor
override fails loudly at build time rather than silently shipping a
discount. `PodVariant` (the selection-output twin) carries the same field
through so pod/build.py never has to re-open the catalog to find it.
`PodDroppedVariant.format` (also new) records a per-VARIANT drop (a size
dropped by DPI or price inside an otherwise-surviving product type) instead
of only ever reporting the whole product type as one drop -- the
carry-forward fix design §13 slice 2 flagged (a portrait 30x40 failing DPI
while 16x20 shipped was previously invisible)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GelatoConfig",
    "PodBuildReport",
    "PodCatalogVariant",
    "PodConfig",
    "PodDropReason",
    "PodDroppedVariant",
    "PodOrientation",
    "PodProductCatalog",
    "PodProviderCatalog",
    "PodRoutingRule",
    "PodVariant",
    "ReprintReason",
    "ReprintResult",
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
    # PRICING DECISION 2026-08-04 (design §5): when set, this IS the retail
    # price -- the cost*markup/floor solve (pod/pricing.py::retail_price) is
    # bypassed entirely. enforce_floor still runs against it, so a value the
    # operator sets below either margin floor is a config error, not a
    # silent discount.
    retail_override: float | None = None


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


class GelatoConfig(BaseModel):
    """Phase C1 scaffolding for the live Gelato adapter (Phase C3). Kept
    separate from `catalog["gelato"]` (PodProviderCatalog, per-product-type
    template_id + priced variants) -- this block is provider account/store
    wiring, not catalog data. Defaults match the shipped pod.json
    placeholders so PodConfig instances built by hand (test helpers
    predating this block) keep validating without carrying it."""

    store_id: str = "REPLACE_AT_C3_gelato_store_id"
    poll_max: int = 10
    poll_interval_seconds: int = 0


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
    gelato: GelatoConfig = Field(default_factory=GelatoConfig)


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
    # Carried through from PodCatalogVariant so pod/build.py's pricing pass
    # doesn't have to re-open the catalog to find it (design §5, PRICING
    # DECISION 2026-08-04).
    retail_override: float | None = None


class PodDroppedVariant(BaseModel):
    """product_type is None only for a photo-wide drop (no candidate product
    type was ever considered -- an aspect miss, or an aspect class with an
    empty/missing formats_by_aspect entry); otherwise it names the dropped
    candidate product type. "orientation" (aspect class matched, but no
    same-aspect variant accepts this photo's orientation), "no_route" (no
    routing rule matched this product_type+region) and "no_variant" (a rule
    matched, but no routed provider stocks a same-aspect-and-orientation
    variant) imply different operator repairs, so they are reported
    separately.

    `format` (slice 2, carry-forward fix design §13 slice 2 flagged) names
    the SPECIFIC size dropped inside an otherwise-surviving product type --
    e.g. a 30x40 that failed DPI, or a size priced above max_price
    ("above_max_price", reachable for the first time once pod/build.py
    actually prices a variant), while its siblings still shipped. It stays
    None for a whole-product-type drop (every reason below "dpi" in
    _REASON_PRECEDENCE can only ever be whole-product-type, since there was
    no specific variant to point at)."""

    product_type: str | None
    format: str | None = None
    reason: PodDropReason


class PodBuildReport(BaseModel):
    """`shopsteward pod build`'s report (BuildReport, listings/models.py,
    twin). Slice 2 stops at print_file_hosted -- no create/link/enrich
    counters until slice 3/4."""

    drafts_built: int = 0
    variants_priced: int = 0
    print_files_hosted: int = 0
    pod_skipped: int = 0
    skipped_idempotent: int = 0


# --- single-photo reprint builder (design 2026-08-11-source-asset-head,
# gap-fill step 1) ------------------------------------------------------------

# Every no-op reason build_pod_reprint can return instead of raising (the
# caller -- the next slice's governed capability -- surfaces these to the
# operator; a stable alias keeps them from drifting the way PodDropReason
# guards against). "not_eligible" mirrors pod_skipped's aspect/dpi/no_route
# family collapsed to one value: a reprint result carries no dropped[] detail
# list of its own, so there is nothing finer to report here.
ReprintReason = Literal[
    "not_archived", "already_exists", "unknown_type", "no_dimensions", "not_eligible"
]


class ReprintResult(BaseModel):
    """build_pod_reprint's return value (pod/build.py). `built=False` is the
    idempotent/precondition-failure no-op path -- never an exception -- so
    the caller can branch on `reason` instead of catching. `draft_id` is set
    whenever a draft (new or pre-existing) can be named: on `built=True` and
    on `reason="already_exists"`, so the caller can find/re-use it."""

    built: bool
    reason: ReprintReason | None = None
    draft_id: str | None = None
    product_type: str | None = None
