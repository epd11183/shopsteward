"""POD build orchestrator, slice 2 (design §1/§3/§13 slice 2): for every
eligible PHOTO (not landing file -- see below), select variants -> price
them -> resolve + host the print file, emitting listingdraft.created ->
.variants_selected -> .priced -> .print_file_prepared -> .print_file_hosted.
STOPS THERE -- provider create/poll/link/enrich is slice 3/4.

Idempotent by `draft_id = sha256(photo_key | pod_config_hash | provider |
product_type)` -- a distinct id space from digital drafts (design §3,
CORRECTED 2026-08-04). One draft per (photo_key, product_type):
catalog.select_variants guarantees every surviving variant of a product type
shares one provider, so one provider PRODUCT (Gelato create-from-template,
slice 3) covers every size that cleared selection+pricing.

`photo_key = photo_id` when the landing row matched a known photo, else the
landing file's own file_id (an unmatched manual drop, decision 33). This is
NOT the same as "one draft per landing file": the Gate 2 export preset lands
BOTH an AdobeRGB TIFF master and an sRGB JPEG per photo (M2b §17 Q13) as TWO
separate proj_landing_files rows sharing photo_id/base_name but different
file_ids. Keying draft_id on landing_file_id (the original slice-2 shape)
built TWO drafts, TWO Gelato products and TWO live Etsy listings of the same
photograph -- exactly the duplicate §3's idempotency exists to prevent,
reached through a door it wasn't watching. Fix: group eligible rows by
photo_key BEFORE selecting/pricing, not per row; print_file.prefer's sibling
lookup (printfile.resolve_print_source_path) already finds the TIFF/JPEG
pair by base_name/photo_id, so it now drives which row supplies the print
master rather than sitting downstream of a per-file loop.

Needs no mockup set: POD images are an enrichment-stage (slice 4) concern,
not a build-stage one.
"""

import hashlib
import sqlite3
from datetime import date

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.adapters.printfile.interface import PrintFileHost
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings import archive as asset_archive
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.models import AssetStoreConfig, PricingRules
from shopsteward.pipeline.listings.pod import catalog, printfile
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod import pricing as pod_pricing
from shopsteward.pipeline.listings.pod.models import (
    PodBuildReport,
    PodConfig,
    PodDroppedVariant,
    PodVariant,
    ReprintResult,
)
from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline

_HOST_NAMES = {"FakePrintFileHost": "fake", "LiveR2PrintFileHost": "cloudflare_r2"}


def _host_name(host: PrintFileHost) -> str:
    return _HOST_NAMES.get(type(host).__name__, type(host).__name__)


def _eligible_landing_rows(
    conn: sqlite3.Connection, user_id: int, photo_id: str | None
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT file_id, path, photo_id, format, width, height FROM proj_landing_files "
        "WHERE user_id=? AND status='valid' ORDER BY file_id",
        (user_id,),
    ).fetchall()
    if photo_id is None:
        return rows
    return [
        row
        for row in rows
        if row["photo_id"] == photo_id
        or (row["photo_id"] is None and f"file-{row['file_id'][:12]}" == photo_id)
    ]


def _photo_key(row: sqlite3.Row) -> str:
    return row["photo_id"] or row["file_id"]


def _eligible_photo_groups(
    conn: sqlite3.Connection, user_id: int, photo_id: str | None
) -> list[list[sqlite3.Row]]:
    """Groups _eligible_landing_rows by photo_key (see module docstring) so
    a TIFF+JPEG sibling pair collapses to ONE group. Rows within a group,
    and the groups themselves, are sorted by file_id -- draft_id keys on
    photo_key alone so ordering never changes the id, but every OTHER choice
    this module makes from a group (which row is "representative") must
    still be deterministic regardless of dict/scan order."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in _eligible_landing_rows(conn, user_id, photo_id):
        groups.setdefault(_photo_key(row), []).append(row)
    return [sorted(groups[key], key=lambda r: r["file_id"]) for key in sorted(groups)]


def _representative_row(rows: list[sqlite3.Row], prefer: str) -> sqlite3.Row:
    """Deterministically picks one row of a photo group to source
    width/height and the `landing_file_id` recorded on events -- the actual
    print master is resolved separately (printfile.resolve_print_source_path
    finds the sibling by base_name/photo_id regardless of which row is
    passed in), so this choice affects display fields only, never draft_id
    or which bytes get printed. Prefers the row already in pod.json's
    preferred format; ties break on the lowest file_id."""
    wanted = printfile._PREFERRED_FORMAT.get(prefer)
    ordered = sorted(rows, key=lambda r: r["file_id"])
    if wanted is not None:
        for row in ordered:
            if row["format"] == wanted:
                return row
    return ordered[0]


def _cost_stale(cfg: PodConfig) -> bool:
    try:
        verified = date.fromisoformat(cfg.costs_verified_on)
    except ValueError:
        return True  # an unparseable date is conservatively treated as stale
    return (date.today() - verified).days > cfg.cost_staleness_days


def _existing_draft(conn: sqlite3.Connection, user_id: int, draft_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT state, print_file_key FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (user_id, draft_id),
    ).fetchone()


def _price_variant(
    variant: PodVariant, cfg: PodConfig, listing_pricing: PricingRules
) -> tuple[float, float, float, float]:
    """Returns (unit_cost, price, net, margin_pct). Raises BelowFloor
    (pod/pricing.py) if an operator's retail_override, or a bug in the
    closed-form solve, fails to clear either margin floor -- that must fail
    loudly, never be caught and silently discounted (design §5, PRICING
    DECISION 2026-08-04)."""
    unit_cost = variant.base_cost + (variant.shipping_est if cfg.pricing.shipping_included else 0.0)
    price = (
        variant.retail_override
        if variant.retail_override is not None
        else pod_pricing.retail_price(unit_cost, cfg.pricing, listing_pricing)
    )
    pod_pricing.enforce_floor(price, unit_cost, cfg.pricing, listing_pricing)
    econ = pod_pricing.pod_economics(price, unit_cost, listing_pricing)
    return unit_cost, price, econ.net, econ.margin_pct


def _select_and_price(
    width: int, height: int, cfg: PodConfig, listing_pricing: PricingRules
) -> tuple[list[PodVariant], list[PodDroppedVariant], dict[str, tuple[float, float, float, float]]]:
    """Shared by build_pod_drafts (which events the result) and
    dry_run_pod_build (which only prints it): select -> price every
    survivor -> drop whichever ends up above max_price (design §5 steps
    1-4). Returns (kept, dropped, priced) where priced maps
    PodVariant.format -> (unit_cost, price, net, margin_pct)."""
    kept, dropped = catalog.select_variants(width, height, cfg)

    priced: dict[str, tuple[float, float, float, float]] = {
        v.format: _price_variant(v, cfg, listing_pricing) for v in kept
    }
    prices = {fmt: values[1] for fmt, values in priced.items()}
    kept, dropped = catalog.apply_price_ceiling(kept, dropped, prices, cfg.pricing.max_price)
    return kept, dropped, priced


def _resolve_pod_skipped_reason(dropped: list[PodDroppedVariant]) -> str:
    # "aspect" (the whole photo doesn't match any configured aspect class)
    # is always select_variants' SOLE dropped entry when it fires -- it
    # isn't in catalog._resolve_reason's precedence tuple, since every other
    # reason there is scoped to one candidate product type, not the whole
    # photo.
    reasons = {d.reason for d in dropped}
    if not reasons or reasons == {"aspect"}:
        return "aspect"
    return catalog._resolve_reason(reasons)


def dry_run_pod_build(
    conn: sqlite3.Connection, user_id: int, *, photo_id: str | None = None
) -> list[dict]:
    """Read-only preview (`shopsteward pod build --dry-run`): the same
    selection + pricing computation build_pod_drafts runs, with NOTHING
    appended to the event log and no print file ever read or hosted. One
    result dict per eligible landing file: {landing_file_id, photo_id,
    kept: [...], dropped: [...]}."""
    pod_config.seed(conn, user_id)
    listing_config.seed(conn, user_id)
    rebuild_pipeline(conn)
    rebuild_pod_config(conn)
    rebuild_listings(conn)

    cfg = pod_config.get_pod_config(conn, user_id)
    if not cfg.enabled:
        return []
    listing_cfg = listing_config.get_config(conn, user_id)

    results: list[dict] = []
    for group in _eligible_photo_groups(conn, user_id, photo_id):
        row = _representative_row(group, cfg.print_file.prefer)
        width, height = row["width"], row["height"]
        if width is None or height is None:
            continue
        kept, dropped, priced = _select_and_price(width, height, cfg, listing_cfg.pricing)
        results.append(
            {
                "landing_file_id": row["file_id"],
                "photo_id": row["photo_id"],
                "kept": [
                    {
                        "product_type": v.product_type,
                        "format": v.format,
                        "size": v.size,
                        "unit_cost": priced[v.format][0],
                        "retail_price": priced[v.format][1],
                        "net": priced[v.format][2],
                        "margin_pct": priced[v.format][3],
                    }
                    for v in kept
                ],
                "dropped": [
                    {"product_type": d.product_type, "format": d.format, "reason": d.reason}
                    for d in dropped
                ],
            }
        )
    return results


def build_pod_drafts(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    photo_id: str | None = None,
    force: bool = False,
    print_file_host: PrintFileHost | None = None,
) -> PodBuildReport:
    pod_config.seed(conn, user_id)
    listing_config.seed(conn, user_id)
    asset_store_config.seed(conn, user_id)
    rebuild_pipeline(conn)
    rebuild_pod_config(conn)
    rebuild_listings(conn)

    cfg = pod_config.get_pod_config(conn, user_id)
    report = PodBuildReport()
    if not cfg.enabled:
        return report

    cfg_hash = pod_config.pod_config_hash(cfg)
    listing_cfg = listing_config.get_config(conn, user_id)
    asset_cfg = asset_store_config.get_asset_store_config(conn, user_id)
    host = print_file_host if print_file_host is not None else FakePrintFileHost()
    host_name = _host_name(host)
    cost_stale = _cost_stale(cfg)

    for group in _eligible_photo_groups(conn, user_id, photo_id):
        row = _representative_row(group, cfg.print_file.prefer)
        photo_key = _photo_key(row)
        landing_file_id = row["file_id"]
        width, height = row["width"], row["height"]
        if width is None or height is None:
            continue

        kept, dropped, priced = _select_and_price(width, height, cfg, listing_cfg.pricing)

        if not kept:
            reason = _resolve_pod_skipped_reason(dropped)
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.pod_skipped",
                    payload={"landing_file_id": landing_file_id, "reason": reason},
                ),
            )
            report.pod_skipped += 1
            continue

        by_product_type: dict[str, list[PodVariant]] = {}
        for variant in kept:
            by_product_type.setdefault(variant.product_type, []).append(variant)

        for product_type, variants in by_product_type.items():
            provider = variants[0].provider
            draft_id = hashlib.sha256(
                f"{photo_key}|{cfg_hash}|{provider}|{product_type}".encode()
            ).hexdigest()
            existing = _existing_draft(conn, user_id, draft_id)

            if existing is not None and existing["state"] == "published":
                report.skipped_idempotent += 1
                continue
            if existing is not None and not force and existing["print_file_key"] is not None:
                report.skipped_idempotent += 1
                continue

            if existing is None or force:
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="listingdraft.created",
                        payload={
                            "draft_id": draft_id,
                            "landing_file_id": landing_file_id,
                            "photo_id": row["photo_id"],
                            "set_key": None,
                            "provider": provider,
                            "format": product_type,
                            "sku_source": "provider",
                            "listing_type": "physical",
                            "config_hash": None,
                            "pod_config_hash": cfg_hash,
                        },
                    ),
                )
                report.drafts_built += 1

            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.variants_selected",
                    payload={
                        "draft_id": draft_id,
                        "aspect": variants[0].aspect,
                        "source_px": [width, height],
                        "variants": [
                            {
                                "format": v.format,
                                "size": v.size,
                                "aspect": v.aspect,
                                "variant_key": v.variant_key,
                                "dpi": v.dpi,
                            }
                            for v in variants
                        ],
                        # The FULL photo-level dropped[] (design §3: "dropped
                        # keys on product_type so the two namespaces are
                        # never confused") -- not just this draft's own
                        # product_type. A product type that loses every
                        # variant has no surviving draft to attach its drop
                        # reason to, so without this a photo with >=1
                        # surviving product type would silently lose the
                        # diagnostic for every OTHER one that didn't survive.
                        # Duplicated verbatim across every draft this PHOTO
                        # (not landing file) produces; each entry's own
                        # product_type still disambiguates it. Cheaper than it
                        # looks now that a TIFF+JPEG pair is one photo group
                        # instead of two -- at most one copy per surviving
                        # product type, not per landing file.
                        "dropped": [
                            {"product_type": d.product_type, "format": d.format, "reason": d.reason}
                            for d in dropped
                        ],
                    },
                ),
            )

            priced_variants = []
            unit_costs = []
            for variant in variants:
                unit_cost, price, net, margin_pct = priced[variant.format]
                unit_costs.append(unit_cost)
                priced_variants.append(
                    {
                        "format": variant.format,
                        "base_cost": variant.base_cost,
                        "shipping_est": variant.shipping_est,
                        "retail_price": price,
                        "net": net,
                        "margin_pct": margin_pct,
                    }
                )
                report.variants_priced += 1

            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.priced",
                    payload={
                        "draft_id": draft_id,
                        "currency": cfg.currency,
                        # A product type can carry several sizes at
                        # different provider costs (e.g. acrylic 43.53 /
                        # 62.28 / 71.09) -- design §3's payload has one
                        # scalar unit_cost per draft, so this is the
                        # CHEAPEST surviving variant's unit_cost ("starting
                        # at"), NOT a total or a mean. Full per-size detail
                        # is in `variants[]` below.
                        "unit_cost": min(unit_costs),
                        "variants": priced_variants,
                        "costs_verified_on": cfg.costs_verified_on,
                        "cost_stale": cost_stale,
                        "auto": True,
                    },
                ),
            )

            # Source-asset head (design 2026-08-11-source-asset-head, slice 2):
            # archive the UNTOUCHED original master (never the sellable
            # re-encode below) before it is read for hosting -- a pure
            # side-archive, config-gated + idempotent, so this never changes
            # the draft/push/Gate 3 outputs that follow. Only archives when a
            # real photo_id is known -- resolve_source and the retrieval
            # fallback both key on photo_id (never photo_key, an unmatched
            # manual drop's landing_file_id), so archiving under photo_key
            # would be unreachable dead storage.
            if asset_cfg.enabled:
                source_row = printfile.resolve_print_source_row(
                    conn, user_id, landing_file_id, cfg.print_file.prefer
                )
                if source_row["photo_id"] is not None:
                    try:
                        asset_archive.archive_master(
                            conn,
                            user_id,
                            asset_cfg,
                            photo_id=source_row["photo_id"],
                            source_landing_file_id=landing_file_id,
                            path=source_row["path"],
                            format=source_row["format"],
                            width=source_row["width"],
                            height=source_row["height"],
                        )
                    except OSError as exc:
                        # The archive must never break the operator's actual
                        # listing build (disk-full/permission on mkdir/
                        # copyfile) -- record it and continue; a later
                        # reprint just sees archived=False and reports "not
                        # reprintable". No path/filename in the payload
                        # (OSError.strerror, never str(exc), which can embed
                        # the destination path).
                        append(
                            conn,
                            Event(
                                user_id=user_id,
                                type="asset.archive_failed",
                                payload={
                                    "photo_id": source_row["photo_id"],
                                    "format": source_row["format"],
                                    "error": (
                                        f"{type(exc).__name__}: {exc.strerror}"
                                        if exc.strerror
                                        else type(exc).__name__
                                    ),
                                },
                            ),
                        )

            data, sellable = printfile.prepare_print_file(
                conn, user_id, landing_file_id, cfg.print_file.prefer, cfg.print_file.max_bytes
            )
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.print_file_prepared",
                    payload={
                        "draft_id": draft_id,
                        "source": sellable.source,
                        "sha256": sellable.sha256,
                        "bytes": sellable.bytes,
                        "long_edge_px": max(width, height),
                    },
                ),
            )

            hosted = printfile.publish_print_file(
                host, data, sellable, ttl_seconds=cfg.print_file.host_ttl_seconds
            )
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.print_file_hosted",
                    payload={
                        "draft_id": draft_id,
                        "host": host_name,
                        "file_key": hosted.key,
                        "expires_at": hosted.expires_at,
                        "sha256": sellable.sha256,
                    },
                ),
            )
            report.print_files_hosted += 1

    rebuild_listings(conn)
    return report


def _archived_row_for_photo(
    conn: sqlite3.Connection, user_id: int, photo_id: str, prefer: str
) -> sqlite3.Row | None:
    """The proj_asset_store row to source a reprint's dims/master from --
    _representative_row's precedent, over the archive instead of landing
    rows: prefers pod.json's preferred format (design §7.4), ties break on
    the lowest format name for determinism. None if the photo was never
    archived at all."""
    rows = conn.execute(
        "SELECT format, width, height, source_landing_file_id FROM proj_asset_store "
        "WHERE user_id=? AND photo_id=? ORDER BY format",
        (user_id, photo_id),
    ).fetchall()
    if not rows:
        return None
    wanted = printfile._PREFERRED_FORMAT.get(prefer)
    if wanted is not None:
        for row in rows:
            if row["format"] == wanted:
                return row
    return rows[0]


def build_pod_reprint(
    conn: sqlite3.Connection,
    user_id: int,
    photo_id: str,
    product_type: str,
    *,
    print_file_host: PrintFileHost,
    pod_cfg: PodConfig | None = None,
    asset_cfg: AssetStoreConfig | None = None,
) -> ReprintResult:
    """Gap-fill step 1 (design 2026-08-11-source-asset-head, "How gap-fill
    consumes the head"): build ONE POD draft for an already-ARCHIVED photo in
    a NEW product_type, sourcing dims + the print master from proj_asset_store
    / the archive rather than the landing folder (which may be long gone --
    that is the entire point of a reprint). Reaches the same print_file_hosted
    stopping point build_pod_drafts reaches, with a byte-shape-identical event
    sequence, so the existing link/enrich/push tail carries it on unchanged.

    Builder only -- no provider create/poll/link/enrich here (that is
    link_pod_drafts, unchanged, called later by the governed capability this
    slice does not build). Every precondition failure returns a `built=False`
    ReprintResult instead of raising -- the caller surfaces the reason.

    `asset_cfg`, if given, is accepted for signature symmetry with `pod_cfg`
    and future callers but currently unused: archive resolution runs through
    printfile.resolve_print_source_path, which already re-reads
    asset_store_config itself (printfile.py's own precedent -- not this
    module's to change)."""
    pod_config.seed(conn, user_id)
    listing_config.seed(conn, user_id)
    asset_store_config.seed(conn, user_id)
    rebuild_pipeline(conn)
    rebuild_pod_config(conn)
    rebuild_listings(conn)

    cfg = pod_cfg if pod_cfg is not None else pod_config.get_pod_config(conn, user_id)
    listing_cfg = listing_config.get_config(conn, user_id)

    known_types = {
        product for provider_cat in cfg.catalog.values() for product in provider_cat.products
    }
    if product_type not in known_types:
        return ReprintResult(built=False, reason="unknown_type")

    archived_row = _archived_row_for_photo(conn, user_id, photo_id, cfg.print_file.prefer)
    if archived_row is None:
        return ReprintResult(built=False, reason="not_archived")

    width, height = archived_row["width"], archived_row["height"]
    if width is None or height is None:
        return ReprintResult(built=False, reason="no_dimensions")

    kept, dropped, priced = _select_and_price(width, height, cfg, listing_cfg.pricing)
    variants = [v for v in kept if v.product_type == product_type]
    if not variants:
        return ReprintResult(built=False, reason="not_eligible")

    cfg_hash = pod_config.pod_config_hash(cfg)
    provider = variants[0].provider
    draft_id = hashlib.sha256(
        f"{photo_id}|{cfg_hash}|{provider}|{product_type}".encode()
    ).hexdigest()

    if _existing_draft(conn, user_id, draft_id) is not None:
        return ReprintResult(
            built=False, reason="already_exists", draft_id=draft_id, product_type=product_type
        )

    landing_file_id = archived_row["source_landing_file_id"]
    host = print_file_host
    host_name = _host_name(host)

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.created",
            payload={
                "draft_id": draft_id,
                "landing_file_id": landing_file_id,
                "photo_id": photo_id,
                "set_key": None,
                "provider": provider,
                "format": product_type,
                "sku_source": "provider",
                "listing_type": "physical",
                "config_hash": None,
                "pod_config_hash": cfg_hash,
            },
        ),
    )

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.variants_selected",
            payload={
                "draft_id": draft_id,
                "aspect": variants[0].aspect,
                "source_px": [width, height],
                "variants": [
                    {
                        "format": v.format,
                        "size": v.size,
                        "aspect": v.aspect,
                        "variant_key": v.variant_key,
                        "dpi": v.dpi,
                    }
                    for v in variants
                ],
                # Full photo-level dropped[] -- build_pod_drafts precedent
                # (design §3): every OTHER product_type this photo didn't
                # survive for, so the draft is shape-identical either way.
                "dropped": [
                    {"product_type": d.product_type, "format": d.format, "reason": d.reason}
                    for d in dropped
                ],
            },
        ),
    )

    priced_variants = []
    unit_costs = []
    for variant in variants:
        unit_cost, price, net, margin_pct = priced[variant.format]
        unit_costs.append(unit_cost)
        priced_variants.append(
            {
                "format": variant.format,
                "base_cost": variant.base_cost,
                "shipping_est": variant.shipping_est,
                "retail_price": price,
                "net": net,
                "margin_pct": margin_pct,
            }
        )

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.priced",
            payload={
                "draft_id": draft_id,
                "currency": cfg.currency,
                "unit_cost": min(unit_costs),
                "variants": priced_variants,
                "costs_verified_on": cfg.costs_verified_on,
                "cost_stale": _cost_stale(cfg),
                "auto": True,
            },
        ),
    )

    # No side-archive here (build_pod_drafts's asset.archived step): the
    # master this draft prints from IS an existing archive entry -- there is
    # nothing new to record.
    data, sellable = printfile.prepare_print_file(
        conn, user_id, landing_file_id, cfg.print_file.prefer, cfg.print_file.max_bytes
    )
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.print_file_prepared",
            payload={
                "draft_id": draft_id,
                "source": sellable.source,
                "sha256": sellable.sha256,
                "bytes": sellable.bytes,
                "long_edge_px": max(width, height),
            },
        ),
    )

    hosted = printfile.publish_print_file(
        host, data, sellable, ttl_seconds=cfg.print_file.host_ttl_seconds
    )
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.print_file_hosted",
            payload={
                "draft_id": draft_id,
                "host": host_name,
                "file_key": hosted.key,
                "expires_at": hosted.expires_at,
                "sha256": sellable.sha256,
            },
        ),
    )

    rebuild_listings(conn)
    return ReprintResult(built=True, draft_id=draft_id, product_type=product_type)
