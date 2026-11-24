"""Pre-publish read-back gate for unattended digital listings
(`listing.catalog_expand`, operator decision 2026-11-23: "full auto + gate").

Run on a pushed DRAFT. Two kinds of check, both fail-closed:

1. Structural -- read back from Etsy through the write adapter (never from the
   local DB): state is draft, exactly 5 digital files, 7 images, the title ends
   with "(Digital Download)", 13 tags.
2. Copy-vs-photo -- a vision model gets the photo plus the title/tags/
   description Etsy holds and flags any claim the photo does not show.

`ok` requires a clean structural pass AND a checker verdict that is ok with no
problems. Nothing here raises once a draft exists: an Etsy read error, an
unreadable photo or a checker failure becomes a problem, so the draft is
recorded (event + brief) instead of orphaned. Every run appends
`listingdraft.readback {draft_id, ok, problems}`; checker spend goes to the
`llm.call` ledger (separate from the $20 cap)."""

import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.vision.interface import CopyChecker
from shopsteward.core.events import Event, append

EXPECTED_FILES = 5
EXPECTED_IMAGES = 7
EXPECTED_TAGS = 13
TITLE_SUFFIX = "(Digital Download)"


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
            result = checker.check_copy(
                Path(path).read_bytes(),
                title=title,
                tags=tags,
                description=description,
                model=model,
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
            problems += result.verdict.problems
            if not result.verdict.ok and not result.verdict.problems:
                problems.append("copy check failed without naming a problem")
    except Exception as exc:  # noqa: BLE001 - fail closed: any failure is a problem, never a raise
        problems.append(f"read-back could not complete: {type(exc).__name__}: {str(exc)[:200]}")

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
