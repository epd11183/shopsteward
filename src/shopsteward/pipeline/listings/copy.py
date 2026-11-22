"""Copy-generation stage (design §5): builds CopyInputs from the house style
guide + proj_scores vision verdict (when available) + mockups.json
whatyougot, calls the CopyAdapter (fixture default, OpenRouter live
triple-gated), appends the AI-disclosure line, and emits
listingdraft.copy_generated + llm.call (live only).

Reads proj_landing_files/proj_scores (owned by pipeline) via the same
connection the orchestrator already holds; never imports adapters.etsy or
mockups (editing/mockups-standalone boundaries don't apply here, but the
listings package still owns no projections outside proj_listing_*)."""

import json
import logging
import os
import sqlite3
from pathlib import Path

from shopsteward.adapters.copy.fake import FixtureCopyAdapter
from shopsteward.adapters.copy.interface import CopyAdapter, CopyInputs, CopyParseError, CopyUsage
from shopsteward.adapters.copy.openrouter import OpenRouterCopyAdapter
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.digital_files import FilePlan, plan_sizes
from shopsteward.pipeline.listings.models import ListingConfig, ListingImage
from shopsteward.pipeline.listings.size_guard import undelivered_sizes
from shopsteward.pipeline.llm_ledger import monthly_spend

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_DEFAULTS_DIR = _REPO_ROOT / "config" / "defaults"
_MOCKUPS_CONFIG_PATH = _CONFIG_DEFAULTS_DIR / "mockups.json"
# Wording for the Files-included block + the prompt's sizes clause/rule
# (config over code). Deliberately NOT part of DigitalFilesConfig: that is
# hashed into every built draft's plan.
_WORDING_PATH = _CONFIG_DEFAULTS_DIR / "prompts" / "digital_copy_wording.json"
# After this many refused generations for one draft (since its last
# listingdraft.created) the LLM is no longer called until a --force rebuild.
MAX_COPY_REFUSALS = 3


def build_copy_adapter(cfg: ListingConfig, *, live: bool) -> CopyAdapter:
    """Single construction path for CopyAdapter instances (vision_factory.py
    precedent): fixture mode when not live, otherwise the OpenRouter provider
    named by listing.json copy.provider (only "openrouter" is supported --
    the same one-provider scope as PRD §13 decision 38)."""
    if not live:
        return FixtureCopyAdapter()

    if cfg.copy_.provider != "openrouter":
        raise ValueError(f"unknown copy provider {cfg.copy_.provider!r}")

    prompt_template = (_CONFIG_DEFAULTS_DIR / cfg.copy_.prompt_path).read_text(encoding="utf-8")
    return OpenRouterCopyAdapter(
        api_key=os.environ["OPENROUTER_API_KEY"],
        prompt_template=prompt_template,
        pricing=cfg.copy_.est_cost_per_mtok,
        temperature=cfg.copy_.temperature,
        sizes_wording=load_wording(),
    )


def _orientation(width: int | None, height: int | None) -> str:
    if not width or not height or width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def _whatyougot() -> dict:
    return json.loads(_MOCKUPS_CONFIG_PATH.read_text(encoding="utf-8"))["whatyougot"]


def _whatyougot_physical() -> dict:
    return json.loads(_MOCKUPS_CONFIG_PATH.read_text(encoding="utf-8"))["whatyougot_physical"]


def _physical_format_label(product_format: str | None) -> str:
    """Human product label for a physical POD draft's `format` column
    (canvas/canvas_portrait/acrylic/poster) -> e.g. "canvas print". Feeds
    CopyInputs.format so the copy prompt states the true medium."""
    base = (product_format or "wall art").replace("_", " ")
    return base if base.endswith(("print", "art")) else f"{base} print"


def _disclosure_line() -> str:
    cfg = json.loads(_MOCKUPS_CONFIG_PATH.read_text(encoding="utf-8"))
    return cfg["listing_copy"]["ai_disclosure_line"]


def load_wording() -> dict:
    return json.loads(_WORDING_PATH.read_text(encoding="utf-8"))


def _ratio_label(cfg: ListingConfig, key: str, wording: dict) -> str:
    assert cfg.digital_files is not None
    if key in wording["ratio_labels"]:
        return wording["ratio_labels"][key]
    spec = next((r for r in cfg.digital_files.ratios if r.key == key), None)
    return f"{spec.short:g}:{spec.long:g}" if spec is not None else key


def files_included_block(plan: FilePlan, cfg: ListingConfig) -> str:
    """Deterministic "Files included" text, one line per planned file (wording
    from prompts/digital_copy_wording.json). Appended AFTER the size guard
    runs; push re-checks stored copy, where this block passes by construction
    (it names only sizes the plan delivers)."""
    assert cfg.digital_files is not None
    w = load_wording()
    full = next((r for r in cfg.digital_files.ratios if r.key == "full"), None)
    full_ratio = f"{full.short:g}:{full.long:g}" if full is not None else ""
    lines = [w["files_header"].format(count=len(plan.files))]
    for entry in plan.files:
        if entry.key == "full":
            label = (
                w["full_label"].format(ratio=full_ratio) if entry.sizes else w["full_label_unsized"]
            )
        else:
            label = _ratio_label(cfg, entry.key, w)
        if entry.sizes:
            lines.append(
                w["file_line_sized"].format(
                    label=label, sizes=", ".join(entry.sizes), ppi=cfg.digital_files.ppi
                )
            )
        else:
            lines.append(w["file_line_unsized"].format(label=label))
    return "\n".join(lines)


def copy_sizes_problem(
    cfg: ListingConfig, plan: FilePlan, title: str, description: str, tags: list[str]
) -> list[str]:
    """Undelivered print sizes named in the copy (empty = fine)."""
    assert cfg.digital_files is not None
    return undelivered_sizes([title, description, *tags], plan, cfg.digital_files)


def copy_refusals(conn: sqlite3.Connection, user_id: int, draft_id: str) -> int:
    """copy.refused events for this draft since its last listingdraft.created
    (a --force rebuild re-emits created, which resets the counter)."""
    last_created = max(
        (
            e.id or 0
            for e in read_all(conn, "listingdraft.created")
            if e.user_id == user_id and e.payload.get("draft_id") == draft_id
        ),
        default=0,
    )
    return sum(
        1
        for e in read_all(conn, "copy.refused")
        if e.user_id == user_id
        and e.payload.get("draft_id") == draft_id
        and (e.id or 0) > last_created
    )


def _record_usage(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    cfg: ListingConfig,
    usage: CopyUsage | None,
) -> None:
    if usage is None:
        return
    append(
        conn,
        Event(
            user_id=user_id,
            type="llm.call",
            payload={
                "provider": cfg.copy_.provider,
                "model": cfg.copy_.model,
                "purpose": "listing_copy",
                "draft_id": draft_id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "est_cost_usd": usage.est_cost_usd,
            },
        ),
    )


def _refuse(
    conn: sqlite3.Connection, user_id: int, draft_id: str, reason: str, offending: list[str]
):
    append(
        conn,
        Event(
            user_id=user_id,
            type="copy.refused",
            payload={"draft_id": draft_id, "reason": reason, "offending": offending},
        ),
    )


_DIGITAL_MARKERS = ("digital download", "instant download", "printable")


def _reads_as_digital_download(title: str, description: str) -> bool:
    """True if copy meant for a PHYSICAL listing still describes a digital
    download -- the exact symptom of the 2026-09-03 defect ("(Digital
    Download)" on a Gelato canvas). Case-insensitive substring scan over
    title + description."""
    haystack = f"{title}\n{description}".lower()
    return any(marker in haystack for marker in _DIGITAL_MARKERS)


def _build_inputs(
    conn: sqlite3.Connection,
    user_id: int,
    landing_file_id: str,
    photo_id: str | None,
    cfg: ListingConfig,
    *,
    medium: str = "digital",
    product_format: str | None = None,
    plan: FilePlan | None = None,
) -> CopyInputs:
    house_style = (_CONFIG_DEFAULTS_DIR / cfg.copy_.house_style_path).read_text(encoding="utf-8")

    dims = conn.execute(
        "SELECT width, height FROM proj_landing_files WHERE user_id=? AND file_id=?",
        (user_id, landing_file_id),
    ).fetchone()
    orientation = _orientation(dims["width"] if dims else None, dims["height"] if dims else None)

    score_row = None
    if photo_id is not None:
        score_row = conn.execute(
            "SELECT subject, strongest_room_style, one_risk, rationale FROM proj_scores "
            "WHERE user_id=? AND photo_id=?",
            (user_id, photo_id),
        ).fetchone()

    # Medium-correct facts: a physical POD product (canvas/acrylic/poster) is
    # NOT a digital download. Hardcoding "digital_download" here is exactly
    # what mislabeled the 2026-09-03 Gelato canvases.
    if medium == "physical":
        wyg = _whatyougot_physical()
        format_label = _physical_format_label(product_format)
        is_physical = True
    else:
        wyg = _whatyougot()
        format_label = "digital_download"
        is_physical = False

    return CopyInputs(
        house_style=house_style,
        subject=score_row["subject"] if score_row else None,
        strongest_room_style=score_row["strongest_room_style"] if score_row else None,
        one_risk=score_row["one_risk"] if score_row else None,
        rationale=score_row["rationale"] if score_row else None,
        orientation=orientation,
        format=format_label,
        # A file plan replaces the global size list: the prompt must not claim
        # sizes the files don't deliver (design §7); copy gets the block instead.
        sizes=[] if plan is not None else wyg["sizes"],
        formats=wyg["formats"],
        is_physical=is_physical,
    )


def generate_copy(
    conn: sqlite3.Connection,
    user_id: int,
    draft_id: str,
    landing_file_id: str,
    photo_id: str | None,
    images: list[ListingImage],
    adapter: CopyAdapter,
    cfg: ListingConfig,
    *,
    live: bool,
    soft_cap_usd: float,
    medium: str = "digital",
    product_format: str | None = None,
    plan: FilePlan | None = None,
) -> bool:
    """Appends listingdraft.copy_generated (+ llm.call when usage is present)
    for one draft. Returns False (no event appended) when a live call is
    refused by the shared monthly soft cap -- the draft stays without copy
    and is picked up by the next run's fill-forward.

    `medium` ("digital" | "physical") + `product_format` (the POD draft's
    format column, physical only) make the copy medium-correct: a physical
    POD listing must never be titled or described as a digital download. The
    POD enrich caller passes medium="physical"; the digital build path uses
    the default. If a physical draft's generated copy still reads as a
    digital download (a live model ignoring the prompt), it is refused the
    same retryable way a parse error is -- no mislabeled listing is ever
    written.

    `plan` (digital, digital_files configured) is the draft's recorded file
    plan -- the single source of truth for sizes: copy that names a print size
    the plan does not deliver is refused the same retryable way, and a
    deterministic "Files included" block is appended. plan=None is the
    legacy behaviour, unchanged."""
    if live and monthly_spend(conn, user_id) >= soft_cap_usd:
        logger.warning(
            "monthly llm.call soft cap reached (>= %.2f usd); refusing live listing "
            "copy for draft %s",
            soft_cap_usd,
            draft_id,
        )
        return False

    if copy_refusals(conn, user_id, draft_id) >= MAX_COPY_REFUSALS:
        logger.warning(
            "copy refused %d times for draft %s; not calling the LLM again until a "
            "--force rebuild (needs operator review)",
            MAX_COPY_REFUSALS,
            draft_id,
        )
        return False

    inputs = _build_inputs(
        conn,
        user_id,
        landing_file_id,
        photo_id,
        cfg,
        medium=medium,
        product_format=product_format,
        plan=plan,
    )
    try:
        result = adapter.generate_copy(inputs, model=cfg.copy_.model)
    except CopyParseError as exc:
        # A malformed/truncated provider response is a per-draft, retryable
        # failure -- not a reason to kill the whole run. Same "no event
        # appended, fill-forward picks it up next time" contract as the
        # soft-cap-refusal branch above.
        logger.warning(
            "copy provider returned an unparseable response for draft %s; will retry next run",
            draft_id,
            exc_info=True,
        )
        # Billed-but-unusable: record the spend and count it toward the
        # refusal cap, or a live model that keeps returning junk is retried
        # (and paid for) every run.
        _record_usage(conn, user_id, draft_id, cfg, exc.usage)
        _refuse(conn, user_id, draft_id, "unparseable", [])
        return False

    # Bill the call before any guard: a refused generation was still paid for,
    # and the monthly cap must see it or retries are unbounded.
    _record_usage(conn, user_id, draft_id, cfg, result.usage)

    # Medium guard: a physical POD listing must never carry digital-download
    # copy. If the generated title/description reads as a digital download
    # anyway, refuse it (retryable, no event) rather than write a mislabeled
    # listing -- the structural backstop behind the format-aware inputs above.
    if inputs.is_physical and _reads_as_digital_download(
        result.verdict.title, result.verdict.description
    ):
        logger.warning(
            "copy for PHYSICAL draft %s reads as a digital download; refusing it "
            "(will retry next run) rather than mislabel a physical listing",
            draft_id,
        )
        _refuse(conn, user_id, draft_id, "digital_markers", [])
        return False

    if plan is not None and not inputs.is_physical:
        bad = copy_sizes_problem(
            cfg, plan, result.verdict.title, result.verdict.description, result.verdict.tags
        )
        if bad:
            logger.warning(
                "copy for draft %s names sizes its file plan does not deliver "
                "(offending: %s; plan delivers: %s); refusing it (will retry next run)",
                draft_id,
                ", ".join(bad),
                ", ".join(plan_sizes(plan)) or "none",
            )
            _refuse(conn, user_id, draft_id, "undelivered_sizes", bad)
            return False

    description = result.verdict.description
    if plan is not None and not inputs.is_physical:
        description = f"{description}\n\n{files_included_block(plan, cfg)}"
    # ponytail: M5a always builds the listing images from the M4 mockup set,
    # so "carries room mockups" == "has any images" for this milestone; a
    # per-image template_id check would be needed if a non-mockup image path
    # is ever added.
    carries_mockups = bool(images)
    disclosure_appended = cfg.copy_.append_disclosure and carries_mockups
    if disclosure_appended:
        description = f"{description}\n\n{_disclosure_line()}"

    append(
        conn,
        Event(
            user_id=user_id,
            type="listingdraft.copy_generated",
            payload={
                "draft_id": draft_id,
                "title": result.verdict.title,
                "tags": result.verdict.tags,
                "description": description,
                "materials": result.verdict.materials,
                "model": cfg.copy_.model,
                "provider": cfg.copy_.provider,
                "disclosure_appended": disclosure_appended,
            },
        ),
    )

    return True
