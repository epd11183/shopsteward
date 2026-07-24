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
from shopsteward.adapters.copy.interface import CopyAdapter, CopyInputs
from shopsteward.adapters.copy.openrouter import OpenRouterCopyAdapter
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.models import ListingConfig, ListingImage
from shopsteward.pipeline.llm_ledger import monthly_spend

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG_DEFAULTS_DIR = _REPO_ROOT / "config" / "defaults"
_MOCKUPS_CONFIG_PATH = _CONFIG_DEFAULTS_DIR / "mockups.json"


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
    )


def _orientation(width: int | None, height: int | None) -> str:
    if not width or not height or width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def _whatyougot() -> dict:
    return json.loads(_MOCKUPS_CONFIG_PATH.read_text(encoding="utf-8"))["whatyougot"]


def _disclosure_line() -> str:
    cfg = json.loads(_MOCKUPS_CONFIG_PATH.read_text(encoding="utf-8"))
    return cfg["listing_copy"]["ai_disclosure_line"]


def _build_inputs(
    conn: sqlite3.Connection,
    user_id: int,
    landing_file_id: str,
    photo_id: str | None,
    cfg: ListingConfig,
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

    wyg = _whatyougot()
    return CopyInputs(
        house_style=house_style,
        subject=score_row["subject"] if score_row else None,
        strongest_room_style=score_row["strongest_room_style"] if score_row else None,
        one_risk=score_row["one_risk"] if score_row else None,
        rationale=score_row["rationale"] if score_row else None,
        orientation=orientation,
        format="digital_download",
        sizes=wyg["sizes"],
        formats=wyg["formats"],
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
) -> bool:
    """Appends listingdraft.copy_generated (+ llm.call when usage is present)
    for one draft. Returns False (no event appended) when a live call is
    refused by the shared monthly soft cap -- the draft stays without copy
    and is picked up by the next run's fill-forward."""
    if live and monthly_spend(conn, user_id) >= soft_cap_usd:
        logger.warning(
            "monthly llm.call soft cap reached (>= %.2f usd); refusing live listing "
            "copy for draft %s",
            soft_cap_usd,
            draft_id,
        )
        return False

    inputs = _build_inputs(conn, user_id, landing_file_id, photo_id, cfg)
    result = adapter.generate_copy(inputs, model=cfg.copy_.model)

    description = result.verdict.description
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

    if result.usage is not None:
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
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "est_cost_usd": result.usage.est_cost_usd,
                },
            ),
        )

    return True
