"""Pure functions: photo aspect + orientation -> candidate product types ->
provider routing -> DPI-gated variant selection (design §5). No I/O --
callers pass in an already-loaded PodConfig. Pricing (design §5 step 4's
above_max_price drop) is slice 2's job; that reason value exists on
PodDroppedVariant but is never produced by this module.

Selection, per product type: routing (route()) gives an ordered provider
list for (product_type, region). Each provider is tried in turn; a provider
"wins" a product type as soon as it yields at least one same-aspect,
same-orientation variant that ALSO clears min_dpi -- design §5's "first
provider whose catalog ... contains a variant matching the photo's aspect
and DPI". A product type therefore creates ONE provider product with
potentially MANY variants inside it (every same-aspect size the winning
provider stocks that clears min_dpi, not just the first one -- an operator
listing framed_poster at 16x20/24x30/30x40 must have all three individually
DPI-checked, not just the first).

Orientation is part of the match (review fix-up B, design §5 step 1): a
portrait and a landscape photo of the identical aspect RATIO are not
interchangeable once a `fit_method:"slice"` provider centre-crops the
mismatch, so `aspect_of` returns the photo's orientation and a catalog
variant is dropped if its own declared orientation excludes the photo
(a variant declaring "any" matches either).

If no provider wins, the product type is dropped with exactly one reason,
resolved from every failure encountered across the walk through a single
explicit precedence order (_REASON_PRECEDENCE) rather than assignment-order
shadowing.

Carry-forward fix (design §13 slice 2 note): when a provider DOES win a
product type, the sizes that failed DPI along the way are no longer
discarded -- each is reported individually as a PodDroppedVariant with
`format` set to that specific size, so an operator can tell "30x40 failed
DPI, 16x20 shipped" apart from "the whole product type shipped clean".
Whole-product-type drops (every routed provider struck out) keep the
original one-reason-via-precedence behaviour, `format=None`, unchanged.
pricing above_max_price drops are NOT produced here -- catalog.py has no
knowledge of price; pod/build.py applies that filter after pricing each
kept PodVariant, reusing this exact same model (PodDroppedVariant) and the
same per-variant-vs-whole-type split.
"""

from shopsteward.pipeline.listings.pod.models import (
    PodConfig,
    PodDroppedVariant,
    PodDropReason,
    PodOrientation,
    PodProviderCatalog,
    PodRoutingRule,
    PodVariant,
)

# Highest-precedence reason wins when a product type is dropped after
# encountering more than one failure across its routed providers (design §5
# fix-up). "orientation" sits just below "dpi": like DPI it is a property of
# the photograph the operator cannot fix in config, but unlike DPI it IS
# fixable by adding the other orientation's SKU to the catalog. Slice 2
# inserts "above_max_price" here -- no caller branches on the individual
# reason strings, so adding a row is the whole change.
_REASON_PRECEDENCE: tuple[PodDropReason, ...] = (
    "dpi",
    "orientation",
    "above_max_price",
    "no_route",
    "no_variant",
)


def aspect_of(
    width: int, height: int, aspects: dict[str, float], tolerance: float
) -> tuple[str, PodOrientation] | None:
    """Nearest configured aspect class to width:height (long/short ratio),
    within `tolerance`, paired with the photo's own orientation. None if
    width/height are invalid or no class is within tolerance -- the whole
    photo is unsellable as POD (design §5: "No match -> pod_skipped")."""
    if width <= 0 or height <= 0:
        return None

    orientation: PodOrientation
    if width > height:
        orientation = "landscape"
    elif height > width:
        orientation = "portrait"
    else:
        orientation = "square"

    ratio = max(width, height) / min(width, height)
    best_key: str | None = None
    best_diff = float("inf")
    for key, class_ratio in aspects.items():
        diff = abs(ratio - class_ratio)
        if diff <= tolerance and diff < best_diff:
            best_key, best_diff = key, diff
    if best_key is None:
        return None
    return best_key, orientation


def effective_dpi(long_edge_px: int, long_edge_inches: float) -> float:
    if long_edge_inches <= 0:
        return 0.0
    return long_edge_px / long_edge_inches


def route(
    product_type: str,
    region: str,
    routing: list[PodRoutingRule],
    catalog: dict[str, PodProviderCatalog],
) -> list[str]:
    """First (product_type, region) rule wins (design §5); returns its
    ordered providers[], filtered to providers actually present in the
    catalog (a routing rule naming an unconfigured provider is a config
    typo, not an escalation -- select_variants simply won't find a variant
    there)."""
    for rule in routing:
        if rule.product_type == product_type and rule.region == region:
            return [p for p in rule.providers if p in catalog]
    return []


def select_variants(
    width: int, height: int, config: PodConfig
) -> tuple[list[PodVariant], list[PodDroppedVariant]]:
    """Design §5 steps 1-4: aspect+orientation -> formats_by_aspect -> routed
    provider order -> first provider whose catalog carries a same-aspect,
    same-orientation variant(s) at or above min_dpi. Step 5 (max_price) is
    slice 2's pricing pass.

    Never returns `kept == [] and dropped == []` (review fix-up H): an
    aspect class present in `aspects` but absent from (or empty in)
    `formats_by_aspect` used to fall through silently, dropping the photo
    with no reason recorded anywhere -- dropped[] is the operator's only
    diagnostic, so that case now records "no_route" too."""
    match = aspect_of(width, height, config.aspects, config.print_file.aspect_tolerance)
    if match is None:
        return [], [PodDroppedVariant(product_type=None, reason="aspect")]
    aspect, orientation = match

    long_edge_px = max(width, height)
    kept: list[PodVariant] = []
    dropped: list[PodDroppedVariant] = []

    for product_type in config.formats_by_aspect.get(aspect, []):
        variants, variant_drops, reason = _select_for_product_type(
            product_type, aspect, orientation, long_edge_px, config
        )
        if variants:
            kept.extend(variants)
            dropped.extend(variant_drops)
        else:
            dropped.append(PodDroppedVariant(product_type=product_type, reason=reason))

    if not kept and not dropped:
        dropped.append(PodDroppedVariant(product_type=None, reason="no_route"))

    return kept, dropped


def _select_for_product_type(
    product_type: str,
    aspect: str,
    orientation: PodOrientation,
    long_edge_px: int,
    config: PodConfig,
) -> tuple[list[PodVariant], list[PodDroppedVariant], PodDropReason | None]:
    """Returns (kept, variant_drops, whole_type_reason). Exactly one of
    `kept` / `whole_type_reason` is populated: a non-empty `kept` means some
    provider won this product type, and `variant_drops` names whichever of
    that SAME provider's same-aspect/orientation sizes failed DPI along the
    way (carry-forward fix). An empty `kept` means every routed provider
    struck out -- `variant_drops` is then always [] and `whole_type_reason`
    carries the single precedence-resolved reason, exactly as before."""
    rule_exists = any(
        r.product_type == product_type and r.region == config.region for r in config.routing
    )
    reasons: set[PodDropReason] = set() if rule_exists else {"no_route"}

    for provider in route(product_type, config.region, config.routing, config.catalog):
        product = config.catalog[provider].products.get(product_type)
        if product is None:
            reasons.add("no_variant")
            continue

        same_aspect = [v for v in product.variants if v.aspect == aspect]
        if not same_aspect:
            reasons.add("no_variant")
            continue

        matching = [v for v in same_aspect if v.orientation in (orientation, "any")]
        if not matching:
            reasons.add("orientation")
            continue

        kept_here: list[PodVariant] = []
        variant_drops: list[PodDroppedVariant] = []
        for variant in matching:
            dpi = effective_dpi(long_edge_px, variant.long_edge_inches)
            if dpi < config.print_file.min_dpi:
                variant_drops.append(
                    PodDroppedVariant(
                        product_type=product_type, format=variant.format, reason="dpi"
                    )
                )
                reasons.add("dpi")
                continue
            kept_here.append(
                PodVariant(
                    product_type=product_type,
                    provider=provider,
                    format=variant.format,
                    variant_key=variant.variant_key,
                    placeholder=variant.placeholder,
                    fit_method=variant.fit_method,
                    size=variant.size,
                    aspect=aspect,
                    orientation=variant.orientation,
                    dpi=dpi,
                    template_id=product.template_id,
                    base_cost=variant.base_cost,
                    shipping_est=variant.shipping_est,
                    retail_override=variant.retail_override,
                )
            )
        if kept_here:
            # This provider won the product type overall -- the sizes it
            # dropped along the way (variant_drops) are reported, not
            # discarded (carry-forward fix).
            return kept_here, variant_drops, None

    if rule_exists and not reasons:
        # the rule matched, but every provider it named was absent from the
        # catalog -- route() filtered them all out before the loop ran.
        reasons.add("no_variant")
    return [], [], _resolve_reason(reasons)


def apply_price_ceiling(
    kept: list[PodVariant],
    dropped: list[PodDroppedVariant],
    prices: dict[str, float],
    max_price: float,
) -> tuple[list[PodVariant], list[PodDroppedVariant]]:
    """Design §5 step 4's other half, applied after pricing (pod/build.py
    calls this once every kept variant has a computed retail price --
    catalog.py itself has no knowledge of price). Mirrors the DPI
    carry-forward split exactly: a variant priced above `max_price` inside
    an otherwise-surviving product type is recorded individually (`format`
    set); a product type that loses every variant to this filter falls back
    to one whole-product-type drop (`format=None`), consistent with every
    other reason in this module. `prices` maps PodVariant.format -> the
    price pod/pricing.py computed for it (retail_override or the markup/
    floor solve, already resolved by the caller)."""
    by_product_type: dict[str, list[PodVariant]] = {}
    for variant in kept:
        by_product_type.setdefault(variant.product_type, []).append(variant)

    new_kept: list[PodVariant] = []
    new_dropped: list[PodDroppedVariant] = list(dropped)
    for product_type, variants in by_product_type.items():
        survivors: list[PodVariant] = []
        variant_drops: list[PodDroppedVariant] = []
        for variant in variants:
            if prices[variant.format] > max_price:
                variant_drops.append(
                    PodDroppedVariant(
                        product_type=product_type, format=variant.format, reason="above_max_price"
                    )
                )
            else:
                survivors.append(variant)
        if survivors:
            new_kept.extend(survivors)
            new_dropped.extend(variant_drops)
        else:
            new_dropped.append(
                PodDroppedVariant(product_type=product_type, format=None, reason="above_max_price")
            )
    return new_kept, new_dropped


def _resolve_reason(reasons: set[PodDropReason]) -> PodDropReason:
    for reason in _REASON_PRECEDENCE:
        if reason in reasons:
            return reason
    return "no_variant"  # defensive: reasons should never be empty here
