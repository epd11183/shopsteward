"""Pre-publish read-back gate for unattended listings: digital
(`listing.catalog_expand`, operator decision 2026-11-23: "full auto + gate")
and physical POD (`listing.gapfill_reprint`, design 2026-11-27).

Run on a pushed DRAFT. Two kinds of check, both fail-closed:

1. Structural -- read back from Etsy through the write adapter (never from the
   local DB): state is draft, exactly 5 digital files, 7 images, the title ends
   with "(Digital Download)", 13 tags. `run_physical_readback` has its own
   physical-listing structure, see its docstring.
2. Copy-vs-photo -- a vision model gets the photo plus the title/tags/
   description Etsy holds and flags any claim the photo does not show. The
   same call also reports visible sensor dust in the delivered file (a dust
   verdict fails the gate; the photo is never auto-healed).

`ok` requires a clean structural pass AND a checker verdict that is ok with no
problems. Nothing here raises once a draft exists: an Etsy read error, an
unreadable photo or a checker failure becomes a problem, so the draft is
recorded (event + brief) instead of orphaned. Every run appends
`listingdraft.readback {draft_id, ok, problems}`; checker spend goes to the
`llm.call` ledger (separate from the $20 cap)."""

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from shopsteward.adapters.etsy.interface import EtsyAdapter, EtsyWriteAdapter
from shopsteward.adapters.vision.interface import CopyChecker
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.pod import printfile
from shopsteward.pipeline.listings.pod.models import PodConfig
from shopsteward.pipeline.listings.pod.pricing import enforce_floor
from shopsteward.pipeline.listings.pricing import BelowFloor

EXPECTED_FILES = 5
EXPECTED_IMAGES = 7
EXPECTED_TAGS = 13
TITLE_SUFFIX = "(Digital Download)"
DIGITAL_TITLE_MARK = "Digital Download"


class ReadbackResult(BaseModel):
    draft_id: str
    ok: bool
    problems: list[str] = Field(default_factory=list)


def _gather(
    conn: sqlite3.Connection, user_id: int, draft_id: str, adapter: EtsyWriteAdapter
) -> tuple[list[str], str, list[str], str, str | None]:
    """(structural problems, title, tags, description, source photo path)."""
    row = conn.execute(
        "SELECT d.etsy_listing_id, l.path FROM proj_listing_drafts d "
        "LEFT JOIN proj_landing_files l "
        "ON l.user_id=d.user_id AND l.file_id=d.landing_file_id "
        "WHERE d.user_id=? AND d.draft_id=?",
        (user_id, draft_id),
    ).fetchone()
    if row is None or row["etsy_listing_id"] is None:
        return [f"draft {draft_id} has no pushed Etsy listing"], "", [], "", None
    listing_id = int(row["etsy_listing_id"])
    listing = adapter.get_listing(listing_id)
    files = adapter.list_listing_files(listing_id)

    problems: list[str] = []
    if listing.state != "draft":
        problems.append(f"Etsy state is {listing.state!r}, expected 'draft'")
    if len(files) != EXPECTED_FILES:
        problems.append(f"{len(files)} digital files on Etsy, expected {EXPECTED_FILES}")
    if len(listing.images) != EXPECTED_IMAGES:
        problems.append(f"{len(listing.images)} images on Etsy, expected {EXPECTED_IMAGES}")
    if not listing.title.endswith(TITLE_SUFFIX):
        problems.append(f"title does not end with {TITLE_SUFFIX!r}")
    if len(listing.tags) != EXPECTED_TAGS:
        problems.append(f"{len(listing.tags)} tags on Etsy, expected {EXPECTED_TAGS}")
    return problems, listing.title, listing.tags, listing.description or "", row["path"]


def _copy_check_problems(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    path: str,
    *,
    title: str,
    tags: list[str],
    description: str,
    checker: CopyChecker,
    model: str,
) -> list[str]:
    """Vision copy-vs-photo check shared by both gates: logs the `llm.call`
    spend and returns the problems found."""
    result = checker.check_copy(
        Path(path).read_bytes(), title=title, tags=tags, description=description, model=model
    )
    usage = result.usage
    if usage is not None:
        append(
            conn,
            Event(
                user_id=user_id,
                type="llm.call",
                payload={
                    "feature": "copy_check",
                    "model": usage.model,
                    "draft_id": draft_id,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "est_cost_usd": usage.est_cost_usd,
                },
            ),
        )
    problems = list(result.verdict.problems)
    if result.verdict.dust.strip():
        problems.append(f"sensor dust: {result.verdict.dust.strip()}")
    elif not result.verdict.ok and not result.verdict.problems:
        problems.append("copy check failed without naming a problem")
    return problems


def _record(
    conn: sqlite3.Connection, user_id: int, draft_id: str, problems: list[str]
) -> ReadbackResult:
    out = ReadbackResult(draft_id=draft_id, ok=not problems, problems=problems)
    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.readback",
            payload={"draft_id": draft_id, "ok": out.ok, "problems": out.problems},
        ),
    )
    return out


def run_readback(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    *,
    adapter: EtsyWriteAdapter,
    checker: CopyChecker,
    model: str,
) -> ReadbackResult:
    problems: list[str] = []
    try:
        problems, title, tags, description, path = _gather(conn, user_id, draft_id, adapter)
        if path is None:
            problems.append("source photo for the draft was not found")
        else:
            problems += _copy_check_problems(
                conn,
                user_id,
                draft_id,
                path,
                title=title,
                tags=tags,
                description=description,
                checker=checker,
                model=model,
            )
    except Exception as exc:  # noqa: BLE001 - fail closed: any failure is a problem, never a raise
        problems.append(f"read-back could not complete: {type(exc).__name__}: {str(exc)[:200]}")
    return _record(conn, user_id, draft_id, problems)


def _max_unit_cost(variants: list[dict], pod_cfg: PodConfig) -> tuple[float | None, list[str]]:
    """Max variant unit cost, build.py's own definition (base_cost plus
    shipping_est when pricing.shipping_included)."""
    try:
        costs = [
            v["base_cost"] + (v["shipping_est"] if pod_cfg.pricing.shipping_included else 0.0)
            for v in variants
        ]
    except KeyError as exc:
        return None, [f"draft variants carry no {exc.args[0]!r}; price floor unverifiable"]
    if not costs:
        return None, ["draft has no variants; price floor unverifiable"]
    return max(costs), []


def _physical_structure(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    *,
    adapter: EtsyWriteAdapter,
    read_adapter: EtsyAdapter,
    pod_cfg: PodConfig,
) -> tuple[list[str], str, list[str], str, str | None]:
    """(problems, title, tags, description, source photo path) for a POD draft,
    read back from Etsy (the draft's own variants/images plan is local)."""
    row = conn.execute(
        "SELECT etsy_listing_id, landing_file_id, format, variants_json, images_json "
        "FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (user_id, draft_id),
    ).fetchone()
    if row is None or row["etsy_listing_id"] is None:
        return [f"draft {draft_id} has no linked Etsy listing"], "", [], "", None
    listing_id = int(row["etsy_listing_id"])
    product_type = row["format"]
    listing = adapter.get_listing(listing_id)
    files = adapter.list_listing_files(listing_id)

    problems: list[str] = []
    if listing.state != "draft":
        problems.append(f"Etsy state is {listing.state!r}, expected 'draft'")
    if files:
        problems.append(f"{len(files)} digital files on a physical listing, expected 0")
    if DIGITAL_TITLE_MARK in listing.title:
        problems.append(f"title contains {DIGITAL_TITLE_MARK!r}")
    suffix = pod_cfg.copy_.title_suffix.get(product_type)
    if not suffix:
        problems.append(f"pod.json has no title_suffix for {product_type!r}")
    elif suffix.strip().lower() not in listing.title.lower():
        # pod.json's suffixes carry a leading space and the generated title
        # parenthesises the label ("(Acrylic Print)"): compare the bare words.
        problems.append(f"title does not contain {suffix.strip()!r}")
    if len(listing.tags) != EXPECTED_TAGS:
        problems.append(f"{len(listing.tags)} tags on Etsy, expected {EXPECTED_TAGS}")
    if not 1 <= len(listing.images) <= pod_cfg.images.hard_cap:
        problems.append(
            f"{len(listing.images)} images on Etsy, expected 1..{pod_cfg.images.hard_cap}"
        )
    if any(
        img.get("intent") == "digital_whatyougot" for img in json.loads(row["images_json"] or "[]")
    ):
        problems.append("a digital_whatyougot image is attached")
    if not listing.production_partners:
        problems.append("no production partner declared on the listing")
    if listing.should_auto_renew:
        problems.append("should_auto_renew is on")

    variants = json.loads(row["variants_json"] or "[]")
    inventory = read_adapter.get_listing_inventory(listing_id)
    products = [p for p in inventory.products if not p.is_deleted]
    if len(products) != len(variants):
        problems.append(f"{len(products)} inventory products on Etsy, draft has {len(variants)}")
    unit_cost, cost_problems = _max_unit_cost(variants, pod_cfg)
    problems += cost_problems
    offerings = [o for p in products for o in p.offerings if not o.is_deleted]
    if not offerings:
        problems.append("Etsy inventory has no offerings; price unverifiable")
    elif unit_cost is not None:
        listing_pricing = listing_config.get_config(conn, user_id).pricing
        for offering in offerings:
            try:
                enforce_floor(offering.price, unit_cost, pod_cfg.pricing, listing_pricing)
            except BelowFloor as exc:
                problems.append(f"price below floor: {exc}")
    path = printfile.resolve_print_source_path(
        conn, user_id, row["landing_file_id"], pod_cfg.print_file.prefer
    )
    return problems, listing.title, listing.tags, listing.description or "", path


def run_physical_readback(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    *,
    adapter: EtsyWriteAdapter,
    read_adapter: EtsyAdapter,
    checker: CopyChecker,
    model: str,
    pod_cfg: PodConfig,
) -> ReadbackResult:
    """Pre-publish gate for an unattended POD listing (`listing.gapfill_reprint`,
    design 2026-11-27 §2). Same fail-closed shape and `listingdraft.readback`
    event as `run_readback`. Structure: draft state, 0 digital files, no
    "Digital Download", the product-type title suffix, 13 tags, 1..hard_cap
    images with no digital panel, a DECLARED PRODUCTION PARTNER (load-bearing:
    empty = stays a draft), Gelato's variant structure intact, every Etsy price
    above the margin floor against the maximum variant cost (a template price
    we never write, so a failure is reported, never fixed), auto-renew off."""
    problems: list[str] = []
    try:
        problems, title, tags, description, path = _physical_structure(
            conn, user_id, draft_id, adapter=adapter, read_adapter=read_adapter, pod_cfg=pod_cfg
        )
        if path is None:
            problems.append("source photo for the draft was not found")
        else:
            problems += _copy_check_problems(
                conn,
                user_id,
                draft_id,
                path,
                title=title,
                tags=tags,
                description=description,
                checker=checker,
                model=model,
            )
    except Exception as exc:  # noqa: BLE001 - fail closed: any failure is a problem, never a raise
        problems.append(f"read-back could not complete: {type(exc).__name__}: {str(exc)[:200]}")
    return _record(conn, user_id, draft_id, problems)
