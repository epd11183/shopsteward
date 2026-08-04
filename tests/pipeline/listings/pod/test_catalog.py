from shopsteward.pipeline.listings.pod import catalog
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod.models import (
    PodCatalogVariant,
    PodConfig,
    PodDroppedVariant,
    PodProductCatalog,
    PodProviderCatalog,
    PodRoutingRule,
)


def _variant(**overrides: object) -> PodCatalogVariant:
    base = dict(
        format="framed_poster_16x20",
        size="16x20in",
        aspect="4:5",
        orientation="any",
        long_edge_inches=20,
        variant_key="variant-key",
        placeholder="ImageFront",
        fit_method="slice",
        base_cost=25.00,
        shipping_est=8.00,
    )
    base.update(overrides)
    return PodCatalogVariant(**base)


def _config(
    *,
    aspects: dict[str, float] | None = None,
    formats_by_aspect: dict[str, list[str]] | None = None,
    routing: list[PodRoutingRule] | None = None,
    catalog_: dict[str, PodProviderCatalog] | None = None,
    min_dpi: int = 150,
    aspect_tolerance: float = 0.02,
) -> PodConfig:
    return PodConfig(
        schema="shopsteward.pod/1",
        name="default",
        enabled=True,
        region="US",
        currency="USD",
        print_file={
            "prefer": "tiff_master",
            "max_bytes": 100_000_000,
            "min_dpi": min_dpi,
            "aspect_tolerance": aspect_tolerance,
            "host_ttl_seconds": 86400,
        },
        aspects=aspects if aspects is not None else {"4:5": 1.25, "2:3": 1.5, "1:1": 1.0},
        formats_by_aspect=(
            formats_by_aspect if formats_by_aspect is not None else {"4:5": ["framed_poster"]}
        ),
        routing=(
            routing
            if routing is not None
            else [PodRoutingRule(product_type="framed_poster", region="US", providers=["gelato"])]
        ),
        catalog=(
            catalog_
            if catalog_ is not None
            else {
                "gelato": PodProviderCatalog(
                    store_id_env="GELATO_STORE_ID",
                    products={
                        "framed_poster": PodProductCatalog(
                            template_id="tmpl", variants=[_variant()]
                        )
                    },
                )
            }
        ),
        costs_verified_on="1970-01-01",
        cost_staleness_days=90,
        pricing={
            "markup": 2.6,
            "price_ending": 0.0,
            "margin_floor_abs": 8.0,
            "margin_floor_pct": 0.25,
            "max_price": 250.0,
            "shipping_included": True,
        },
        copy={"title_suffix": {}, "description_block": {}},
        images={"max_ours": 5, "hard_cap": 10, "trim_provider_images": True},
        link_timeout_seconds=180,
        link_poll_interval_seconds=10,
    )


# --- aspect_of ---------------------------------------------------------


def test_aspect_of_exact_match():
    assert catalog.aspect_of(4000, 5000, {"4:5": 1.25}, 0.02) == ("4:5", "portrait")


def test_aspect_of_within_tolerance_picks_nearest_of_two_in_range():
    # ratio 1.24: both classes are within the (loose) 0.06 tolerance
    # (diff 0.04 and 0.06), but "a" is strictly closer -- exercises the
    # `diff < best_diff` tie-break, not just the `diff <= tolerance` gate.
    aspects = {"a": 1.20, "b": 1.30}
    assert catalog.aspect_of(1240, 1000, aspects, 0.06) == ("a", "landscape")


def test_aspect_of_no_match_returns_none():
    assert catalog.aspect_of(1000, 1000, {"4:5": 1.25}, 0.02) is None


def test_aspect_of_square_orientation():
    assert catalog.aspect_of(2000, 2000, {"1:1": 1.0}, 0.02) == ("1:1", "square")


def test_aspect_of_invalid_dimensions_returns_none():
    assert catalog.aspect_of(0, 100, {"4:5": 1.25}, 0.02) is None
    assert catalog.aspect_of(100, 0, {"4:5": 1.25}, 0.02) is None


# --- effective_dpi -------------------------------------------------------


def test_effective_dpi():
    assert catalog.effective_dpi(3000, 20) == 150.0


def test_effective_dpi_zero_inches_is_zero():
    assert catalog.effective_dpi(3000, 0) == 0.0


# --- route ---------------------------------------------------------------


def _provider_catalog() -> PodProviderCatalog:
    return PodProviderCatalog(store_id_env="X_STORE_ID", products={})


def test_route_returns_first_matching_rule_providers():
    routing = [
        PodRoutingRule(product_type="canvas", region="US", providers=["printful", "gelato"]),
    ]
    result = catalog.route(
        "canvas", "US", routing, {"printful": _provider_catalog(), "gelato": _provider_catalog()}
    )
    assert result == ["printful", "gelato"]


def test_route_first_match_wins_over_a_later_rule_for_the_same_key():
    routing = [
        PodRoutingRule(product_type="canvas", region="US", providers=["gelato"]),
        PodRoutingRule(product_type="canvas", region="US", providers=["printful"]),
    ]
    result = catalog.route(
        "canvas", "US", routing, {"gelato": _provider_catalog(), "printful": _provider_catalog()}
    )
    assert result == ["gelato"]


def test_route_filters_providers_absent_from_catalog():
    routing = [
        PodRoutingRule(product_type="canvas", region="US", providers=["printful", "gelato"]),
    ]
    result = catalog.route("canvas", "US", routing, {"gelato": _provider_catalog()})
    assert result == ["gelato"]


def test_route_no_matching_rule_returns_empty():
    routing = [PodRoutingRule(product_type="canvas", region="US", providers=["gelato"])]
    assert catalog.route("framed_poster", "US", routing, {"gelato": _provider_catalog()}) == []


# --- select_variants -------------------------------------------------------


def test_select_variants_aspect_match_kept():
    cfg = _config()
    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert dropped == []
    assert len(kept) == 1
    variant = kept[0]
    assert variant.product_type == "framed_poster"
    assert variant.provider == "gelato"
    assert variant.aspect == "4:5"
    assert variant.dpi == 250.0
    assert variant.variant_key == "variant-key"


def test_select_variants_aspect_miss_is_skipped():
    cfg = _config()
    # ratio 1.7 is outside tolerance of every configured class (4:5=1.25,
    # 2:3=1.5, 1:1=1.0)
    kept, dropped = catalog.select_variants(1700, 1000, cfg)

    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].product_type is None
    assert dropped[0].reason == "aspect"


def test_select_variants_dpi_drop():
    # long_edge_inches=20 with a 1000px long edge -> 50 dpi, below the 150 floor
    cfg = _config(min_dpi=150)
    kept, dropped = catalog.select_variants(800, 1000, cfg)

    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].product_type == "framed_poster"
    assert dropped[0].reason == "dpi"


def test_select_variants_multiple_same_aspect_sizes_are_each_dpi_checked():
    # framed_poster offered at three sizes, all aspect 4:5: 16x20 and 24x30
    # clear the 150 dpi floor at a 5000px long edge, 30x40 does not. The
    # single-`next()` regression this guards against would have silently
    # returned only the first (16x20) and never evaluated the rest.
    variants = [
        _variant(format="framed_poster_16x20", variant_key="v-16x20", long_edge_inches=20),
        _variant(format="framed_poster_24x30", variant_key="v-24x30", long_edge_inches=30),
        _variant(format="framed_poster_30x40", variant_key="v-30x40", long_edge_inches=40),
    ]
    catalog_ = {
        "gelato": PodProviderCatalog(
            store_id_env="GELATO_STORE_ID",
            products={"framed_poster": PodProductCatalog(template_id="tmpl", variants=variants)},
        )
    }
    cfg = _config(catalog_=catalog_, min_dpi=150)

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert dropped == []  # the product type succeeded overall (>=1 survivor)
    assert {v.variant_key for v in kept} == {"v-16x20", "v-24x30"}
    assert all(v.provider == "gelato" for v in kept)
    # 5000px / 40in = 125dpi < 150 -- 30x40 must not appear
    assert "v-30x40" not in {v.variant_key for v in kept}


def test_select_variants_routing_fallback_to_second_provider():
    # gelato stocks framed_poster but only at insufficient DPI for this photo;
    # printful stocks it with enough long_edge_inches to clear the DPI floor.
    routing = [
        PodRoutingRule(product_type="framed_poster", region="US", providers=["gelato", "printful"])
    ]
    catalog_ = {
        "gelato": PodProviderCatalog(
            store_id_env="GELATO_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(
                    template_id="tmpl-gelato",
                    variants=[_variant(long_edge_inches=40, variant_key="gelato-variant")],
                )
            },
        ),
        "printful": PodProviderCatalog(
            store_id_env="PRINTFUL_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(
                    template_id=None,
                    variants=[_variant(long_edge_inches=10, variant_key="printful-variant")],
                )
            },
        ),
    }
    cfg = _config(routing=routing, catalog_=catalog_, min_dpi=150)

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert dropped == []
    assert len(kept) == 1
    assert kept[0].provider == "printful"
    assert kept[0].variant_key == "printful-variant"


def test_select_variants_provider_stocks_product_type_but_no_same_aspect_variant():
    # gelato carries framed_poster, but only in 2:3 -- the photo classifies
    # as 4:5, so this must fall through to "no_variant", not silently match.
    routing = [PodRoutingRule(product_type="framed_poster", region="US", providers=["gelato"])]
    catalog_ = {
        "gelato": PodProviderCatalog(
            store_id_env="GELATO_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(
                    template_id="tmpl", variants=[_variant(aspect="2:3")]
                )
            },
        )
    }
    cfg = _config(routing=routing, catalog_=catalog_)

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].product_type == "framed_poster"
    assert dropped[0].reason == "no_variant"


def test_select_variants_no_route_drop():
    # formats_by_aspect names a product type with no routing rule at all.
    cfg = _config(formats_by_aspect={"4:5": ["acrylic"]})

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].product_type == "acrylic"
    assert dropped[0].reason == "no_route"


def test_select_variants_reason_precedence_prefers_dpi_over_no_variant():
    # A genuinely multi-provider list where one routed provider contributes
    # "no_variant" (no same-aspect stock at all) and another contributes
    # "dpi" (same-aspect stock, but too small) -- pins that the resolved
    # reason is the higher-precedence one regardless of provider order.
    routing = [
        PodRoutingRule(
            product_type="framed_poster", region="US", providers=["no_stock", "too_small"]
        )
    ]
    catalog_ = {
        "no_stock": PodProviderCatalog(
            store_id_env="A_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(
                    template_id="tmpl", variants=[_variant(aspect="2:3")]
                )
            },
        ),
        "too_small": PodProviderCatalog(
            store_id_env="B_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(
                    # 5000px / 100in = 50dpi -- same aspect, but under the
                    # 150dpi floor.
                    template_id="tmpl",
                    variants=[_variant(long_edge_inches=100)],
                )
            },
        ),
    }
    cfg = _config(routing=routing, catalog_=catalog_, min_dpi=150)

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].reason == "dpi"


def test_select_variants_rule_matches_but_no_provider_in_routing_is_configured():
    # rule matches (product_type+region) but every provider it names is
    # absent from the catalog -- route() filters them all out before the
    # loop body ever runs, so `reasons` stays empty and the
    # rule_exists-but-no-reasons fallback branch must supply "no_variant".
    routing = [PodRoutingRule(product_type="framed_poster", region="US", providers=["nope"])]
    cfg = _config(routing=routing)  # default catalog_ is {"gelato": ...}; "nope" isn't in it

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert kept == []
    assert dropped == [PodDroppedVariant(product_type="framed_poster", reason="no_variant")]


def test_select_variants_mixed_kept_and_dropped_across_product_types():
    catalog_ = {
        "gelato": PodProviderCatalog(
            store_id_env="GELATO_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(template_id="tmpl", variants=[_variant()]),
                # "poster" has no product entry at all -> no_variant
            },
        )
    }
    cfg = _config(
        formats_by_aspect={"4:5": ["framed_poster", "poster"]},
        routing=[
            PodRoutingRule(product_type="framed_poster", region="US", providers=["gelato"]),
            PodRoutingRule(product_type="poster", region="US", providers=["gelato"]),
        ],
        catalog_=catalog_,
    )

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert {v.product_type for v in kept} == {"framed_poster"}
    assert dropped == [PodDroppedVariant(product_type="poster", reason="no_variant")]


def test_select_variants_never_returns_all_empty_when_aspect_matched():
    # formats_by_aspect declares the matched aspect class with an empty
    # candidate list -- previously fell through to kept=[]/dropped=[] with
    # no diagnostic recorded anywhere (review fix-up H).
    cfg = _config(formats_by_aspect={"4:5": []})

    kept, dropped = catalog.select_variants(4000, 5000, cfg)

    assert kept == []
    assert dropped == [PodDroppedVariant(product_type=None, reason="no_route")]
    assert not (kept == [] and dropped == [])


# --- orientation (review fix-up B) -----------------------------------------


def test_select_variants_landscape_and_portrait_of_the_same_ratio_are_not_equal():
    # design §5 step 1 fix: a landscape-only variant must not satisfy a
    # portrait photo of the identical aspect ratio (and vice-versa) -- this
    # fails today, because the shipped code discards orientation entirely.
    catalog_ = {
        "gelato": PodProviderCatalog(
            store_id_env="GELATO_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(
                    template_id="tmpl", variants=[_variant(orientation="landscape")]
                )
            },
        )
    }
    cfg = _config(catalog_=catalog_)

    landscape = catalog.select_variants(5000, 4000, cfg)
    portrait = catalog.select_variants(4000, 5000, cfg)

    assert landscape != portrait
    landscape_kept, landscape_dropped = landscape
    assert len(landscape_kept) == 1
    assert landscape_dropped == []

    portrait_kept, portrait_dropped = portrait
    assert portrait_kept == []
    assert portrait_dropped == [
        PodDroppedVariant(product_type="framed_poster", reason="orientation")
    ]


def test_select_variants_any_orientation_matches_either():
    catalog_ = {
        "gelato": PodProviderCatalog(
            store_id_env="GELATO_STORE_ID",
            products={
                "framed_poster": PodProductCatalog(
                    template_id="tmpl", variants=[_variant(orientation="any")]
                )
            },
        )
    }
    cfg = _config(catalog_=catalog_)

    landscape_kept, landscape_dropped = catalog.select_variants(5000, 4000, cfg)
    portrait_kept, portrait_dropped = catalog.select_variants(4000, 5000, cfg)

    assert landscape_dropped == [] and len(landscape_kept) == 1
    assert portrait_dropped == [] and len(portrait_kept) == 1


def test_select_variants_shipped_pod_json_landscape_vs_portrait():
    cfg = pod_config.load_pod_config()

    kept_landscape, dropped_landscape = catalog.select_variants(5000, 4000, cfg)
    assert {v.format for v in kept_landscape} == {"framed_poster_16x20"}
    assert not any(d.reason == "orientation" for d in dropped_landscape)

    kept_portrait, dropped_portrait = catalog.select_variants(4000, 5000, cfg)
    assert kept_portrait == []
    assert any(
        d.product_type == "framed_poster" and d.reason == "orientation" for d in dropped_portrait
    )
