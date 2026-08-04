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
shadowing -- slice 2 adds "above_max_price" to the same accumulation by
adding a table entry, no branching changes needed.
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
        variants, reason = _select_for_product_type(
            product_type, aspect, orientation, long_edge_px, config
        )
        if variants:
            kept.extend(variants)
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
) -> tuple[list[PodVariant], PodDropReason | None]:
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
        for variant in matching:
            dpi = effective_dpi(long_edge_px, variant.long_edge_inches)
            if dpi < config.print_file.min_dpi:
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
                    dpi=dpi,
                    template_id=product.template_id,
                    base_cost=variant.base_cost,
                    shipping_est=variant.shipping_est,
                )
            )
        if kept_here:
            return kept_here, None

    if rule_exists and not reasons:
        # the rule matched, but every provider it named was absent from the
        # catalog -- route() filtered them all out before the loop ran.
        reasons.add("no_variant")
    return [], _resolve_reason(reasons)


def _resolve_reason(reasons: set[PodDropReason]) -> PodDropReason:
    for reason in _REASON_PRECEDENCE:
        if reason in reasons:
            return reason
    return "no_variant"  # defensive: reasons should never be empty here
