"""POD provider create->poll->link (Phase C, slice 3, design §7.1/§3). For
every POD draft that reached `listingdraft.print_file_hosted` (pod/build.py,
slice 2) and has no CONFIRMED provider product (etsy_listing_id IS NULL --
the durable link signal; pod_status is mutable and advances past "linked"
once enrichment runs, so it must never gate the skip),
this builds a `PodProductSpec` from the draft's already-selected+priced
variants, calls `PodAdapter.create_product`, polls `get_product` until
linked/failed/poll_max, and events the outcome. Never raises on one draft's
failure -- a bad catalog entry (canvas's shipped "<OPERATOR>" placeholder) or
an exhausted poll must not stop every OTHER draft from being attempted.

Two config sources, NOT one (see pod/models.py's GelatoConfig docstring):
`cfg.catalog["gelato"]` (PodProviderCatalog, keyed by product_type) already
carries the real template_id + per-variant variant_key/placeholder/
fit_method -- pod/catalog.py's select_variants() reads it, and
listingdraft.variants_selected/.priced already carry format+variant_key+
retail_price forward from there. `cfg.gelato` (GelatoConfig, Phase C1
scaffolding) carries store_id + poll_max/poll_interval_seconds only -- store/
account wiring, not catalog data. So this module re-derives
placeholder/fit_method/template_id from `cfg.catalog["gelato"]` (matched by
product_type + format) rather than duplicating them into events, and reads
store_id/poll_max/poll_interval_seconds from `cfg.gelato`.

`print_file_url` is transient (a rotating signed URL, design §17 Q1a) and is
NEVER read from an event -- it isn't in one. This module re-hosts the print
file (printfile.resolve_print_source_path -> prepare_print_file ->
publish_print_file) immediately before every create_product call to obtain a
fresh one, exactly like pod/build.py does for the FIRST host. A draft whose
provider product already exists (created but not yet confirmed linked -- a
prior run's poll failed/exhausted) is NOT re-hosted or re-created: Gelato
dedupes create by idempotency_key=draft_id, so a second create call would
409; this module instead reuses the stored provider_product_id and only
polls again.
"""

import json
import sqlite3
import time

from pydantic import BaseModel, ValidationError

from shopsteward.adapters.pod.interface import PodAdapter, PodWriteError
from shopsteward.adapters.pod.models import PodProductSpec, PodProviderRef, PodVariantSpec
from shopsteward.adapters.printfile.interface import PrintFileHost
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.pod import printfile
from shopsteward.pipeline.listings.pod.config import resolve_store_id
from shopsteward.pipeline.listings.pod.models import PodConfig
from shopsteward.pipeline.listings.projections import rebuild_listings


class PodLinkReport(BaseModel):
    """`pod link`'s report (PodBuildReport, pod/models.py, twin)."""

    created: int = 0
    linked: int = 0
    failed: int = 0
    skipped_idempotent: int = 0


def _eligible_rows(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT draft_id, landing_file_id, photo_id, format AS product_type, "
        "variants_json, currency, provider_product_id, pod_status, etsy_listing_id "
        "FROM proj_listing_drafts WHERE user_id=? AND pod_config_hash IS NOT NULL "
        "AND print_file_key IS NOT NULL ORDER BY draft_id",
        (user_id,),
    ).fetchall()


def _catalog_variant_lookup(cfg: PodConfig, product_type: str) -> tuple[str | None, dict]:
    """(template_id, {format: PodCatalogVariant}) for `product_type` from
    `cfg.catalog["gelato"]` -- the ONE place placeholder/fit_method/
    template_id live (see module docstring)."""
    product = cfg.catalog.get("gelato")
    if product is None:
        return None, {}
    catalog_product = product.products.get(product_type)
    if catalog_product is None:
        return None, {}
    return catalog_product.template_id, {v.format: v for v in catalog_product.variants}


def _build_spec(
    conn: sqlite3.Connection,
    user_id: int,
    row: sqlite3.Row,
    cfg: PodConfig,
    host: PrintFileHost,
) -> PodProductSpec:
    template_id, by_format = _catalog_variant_lookup(cfg, row["product_type"])

    variant_specs: list[PodVariantSpec] = []
    for v in json.loads(row["variants_json"] or "[]"):
        catalog_variant = by_format.get(v["format"])
        variant_specs.append(
            PodVariantSpec(
                format=v["format"],
                variant_key=v["variant_key"],
                placeholder=catalog_variant.placeholder if catalog_variant else None,
                fit_method=catalog_variant.fit_method if catalog_variant else None,
                retail_price=v["retail_price"],
            )
        )

    ref = PodProviderRef(
        provider="gelato",
        store_id=resolve_store_id(cfg),
        template_id=template_id,
        variants=variant_specs,
    )

    # Minimal deterministic placeholder copy -- C2 enrichment overwrites
    # title/description once real copy exists (listingdraft.copy_generated);
    # this only has to satisfy PodProductSpec's min_length=1 at create time.
    subject = row["photo_id"] or row["landing_file_id"]
    title = f"{subject} - {row['product_type']}"
    description = f"POD {row['product_type']} product for {subject}."

    data, sellable = printfile.prepare_print_file(
        conn, user_id, row["landing_file_id"], cfg.print_file.prefer, cfg.print_file.max_bytes
    )
    hosted = printfile.publish_print_file(
        host, data, sellable, ttl_seconds=cfg.print_file.host_ttl_seconds
    )

    return PodProductSpec(
        ref=ref,
        title=title,
        description=description,
        tags=[],
        print_file_url=hosted.url,
        idempotency_key=row["draft_id"],
        publish_as_draft=True,
    )


def link_pod_drafts(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    adapter: PodAdapter,
    print_file_host: PrintFileHost,
    cfg: PodConfig,
    poll_max: int | None = None,
    force: bool = False,
) -> PodLinkReport:
    report = PodLinkReport()
    max_polls = poll_max if poll_max is not None else cfg.gelato.poll_max
    poll_interval = cfg.gelato.poll_interval_seconds

    for row in _eligible_rows(conn, user_id):
        if not force and row["etsy_listing_id"] is not None:
            report.skipped_idempotent += 1
            continue

        draft_id = row["draft_id"]
        provider_product_id = row["provider_product_id"]

        if provider_product_id is None:
            # Never raise on one draft's failure (a bad catalog entry -- the
            # shipped canvas template_id is still "<OPERATOR>" -- or any
            # other spec-construction problem must not stop the rest).
            try:
                spec = _build_spec(conn, user_id, row, cfg, print_file_host)
                product = adapter.create_product(spec)
            except (ValidationError, PodWriteError, LookupError) as exc:
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="listingdraft.provider_failed",
                        payload={"draft_id": draft_id, "reason": str(exc)[:500]},
                    ),
                )
                report.failed += 1
                continue

            provider_product_id = product.provider_product_id
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.provider_created",
                    payload={
                        "draft_id": draft_id,
                        "provider_product_id": provider_product_id,
                        "variant_count": product.variant_count,
                    },
                ),
            )
            report.created += 1

        # None until the poll loop resolves the draft one way or another;
        # "linked" / "reported_failed" mean an outcome event was already
        # appended inside the loop, so the exhausted-poll fallback below
        # must not append a SECOND provider_failed for the same draft.
        outcome: str | None = None
        for attempt in range(max_polls):
            try:
                product = adapter.get_product(provider_product_id)
            except PodWriteError as exc:
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="listingdraft.provider_failed",
                        payload={"draft_id": draft_id, "reason": str(exc)[:500]},
                    ),
                )
                report.failed += 1
                outcome = "reported_failed"
                break

            if product.status == "linked":
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="listingdraft.provider_linked",
                        payload={
                            "draft_id": draft_id,
                            "etsy_listing_id": product.etsy_listing_id,
                            "etsy_listing_state": product.etsy_listing_state,
                        },
                    ),
                )
                report.linked += 1
                outcome = "linked"
                break
            if product.status == "failed":
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="listingdraft.provider_failed",
                        payload={
                            "draft_id": draft_id,
                            "reason": product.error or "provider reported failed",
                        },
                    ),
                )
                report.failed += 1
                outcome = "reported_failed"
                break

            if poll_interval and attempt < max_polls - 1:
                time.sleep(poll_interval)

        if outcome is None:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.provider_failed",
                    payload={"draft_id": draft_id, "reason": "poll_exhausted"},
                ),
            )
            report.failed += 1

    rebuild_listings(conn)
    return report
