"""`listings reset` (design: winners-batch reset): un-poisons a
proj_listing_drafts row so `listings build` / `listings push` can run again
against the same landing file, without ever touching the numeric external
ids (a fake dry-run push writes a fake etsy_listing_id like 5052 -- the
system cannot tell that apart from a real one, so every safety rail here is
behavioral: state/adopted-ness/presence-of-an-external-id, never a number).

Landing-row reset always ships paired with a re-observe (scan_landing) in
the same --apply call, never left bare: push._landing_path() raises
LookupError for a landing_file_id with no proj_landing_files row, which
would make every draft using that file permanently ineligible.

Mockups are untouched on purpose (design §4): draft_id = sha256(landing_
file_id | config_hash | set_key) and the mockup set_key = sha256(landing_
file_id | config_hash | template_library_hash) both depend only on inputs a
reset doesn't change, so the existing mockup set is reused and a rebuild
reproduces the identical draft_id.

Operator-invoked CLI only (pipeline/listings/cli.py's `reset` command) --
never called from shop.py's autonomous build orchestration.
"""

import sqlite3
from pathlib import Path

from shopsteward.core.events import Event, append
from shopsteward.pipeline import landing
from shopsteward.pipeline.listings.models import ResetPlanRow, ResetReport
from shopsteward.pipeline.listings.projections import rebuild_listings


def _verdict(row: sqlite3.Row, *, include_pushed: bool) -> str:
    if row["state"] == "published":
        return "refused_published"
    if row["state"] == "adopted" or row["draft_id"].startswith("adopted-"):
        return "refused_adopted"
    has_external_id = row["etsy_listing_id"] is not None or row["provider_product_id"] is not None
    if has_external_id and not include_pushed:
        return "needs_confirmation"
    return "reset"


def plan_reset(
    conn: sqlite3.Connection, user_id: int, folder: Path, *, include_pushed: bool
) -> list[ResetPlanRow]:
    """Read-only: re-hashes files in `folder` the same way landing.py does,
    finds proj_listing_drafts rows built from a file currently in that
    folder, and classifies each into a verdict. Never writes an event."""
    rebuild_listings(conn)

    file_ids = list(set(landing.folder_file_ids(folder).values()))
    if not file_ids:
        return []

    placeholders = ",".join("?" for _ in file_ids)
    rows = conn.execute(
        "SELECT draft_id, landing_file_id, state, etsy_listing_id, provider_product_id, "
        "pod_status FROM proj_listing_drafts WHERE user_id=? AND landing_file_id IN "
        f"({placeholders}) ORDER BY draft_id",
        (user_id, *file_ids),
    ).fetchall()

    return [
        ResetPlanRow(
            draft_id=row["draft_id"],
            landing_file_id=row["landing_file_id"],
            state=row["state"],
            etsy_listing_id=row["etsy_listing_id"],
            provider_product_id=row["provider_product_id"],
            pod_status=row["pod_status"],
            verdict=_verdict(row, include_pushed=include_pushed),
        )
        for row in rows
    ]


class ResetIncomplete(RuntimeError):
    """Raised by apply_reset when the post-reset re-observe leaves a file
    worse off than before (still poisoned, but also unmatched now) -- a
    recovery tool must never report success while leaving that behind."""


def apply_reset(
    conn: sqlite3.Connection,
    user_id: int,
    plan: list[ResetPlanRow],
    *,
    folder: Path,
    reason: str,
    keep_landing: bool,
) -> ResetReport:
    """Re-validates each row against the CURRENT proj_listing_drafts row
    before resetting it -- never trusts plan[i].verdict alone, which may be
    stale (a race with a concurrent `shop build`/push, or in a test,
    deliberately forced):
      - state='published' or an "adopted-*" draft_id is always refused,
        regardless of what the plan said.
      - a row's etsy_listing_id/provider_product_id must be UNCHANGED from
        what the plan recorded -- a plan-time "reset" verdict for a
        never-pushed row does not authorize resetting a row that acquired a
        real Etsy listing id in the meantime, and a plan-time "reset"
        verdict for a confirmed pushed row (--include-pushed +
        --confirm-listing-id, checked by the CLI against the plan's ids)
        does not authorize resetting it if that id has since changed.
    Confirmation-id matching itself (needs_confirmation/refused_* verdicts)
    is the CLI's job before calling this -- apply_reset only ever proceeds
    on plan rows already marked verdict=="reset"."""
    report = ResetReport()
    landing_file_ids: set[str] = set()

    for row in plan:
        if row.verdict != "reset":
            continue
        current = conn.execute(
            "SELECT draft_id, landing_file_id, state, etsy_listing_id, provider_product_id, "
            "pod_status FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
            (user_id, row.draft_id),
        ).fetchone()
        if current is None:
            continue  # already gone
        if current["state"] == "published":
            continue  # hard refusal, no override
        if current["state"] == "adopted" or current["draft_id"].startswith("adopted-"):
            continue  # hard refusal, no override -- use archive adopt-local --revoke
        if (
            current["etsy_listing_id"] != row.etsy_listing_id
            or current["provider_product_id"] != row.provider_product_id
        ):
            continue  # stale plan: external id changed since plan_reset -- re-plan and re-confirm

        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.reset",
                payload={
                    "draft_id": current["draft_id"],
                    "landing_file_id": current["landing_file_id"],
                    "prior_state": current["state"],
                    "prior_etsy_listing_id": current["etsy_listing_id"],
                    "prior_provider_product_id": current["provider_product_id"],
                    "prior_pod_status": current["pod_status"],
                    "reason": reason,
                },
            ),
        )
        report.drafts_reset += 1
        if current["landing_file_id"] is not None:
            landing_file_ids.add(current["landing_file_id"])

    rebuild_listings(conn)

    if not keep_landing:
        for file_id in landing_file_ids:
            landing.reset_file(conn, user_id, file_id, reason=reason)
            report.landing_files_reset += 1
        # Never leave a bare delete: a landing_file_id with no
        # proj_landing_files row makes push._landing_path() raise, so every
        # draft built from it becomes permanently ineligible. Re-observing
        # in the same call refreshes path/width/height if the winners
        # folder moved, and gives back the SAME file_id (unchanged bytes).
        report.landing = landing.scan_landing(conn, user_id, folder)

        # This is a recovery tool -- it must not have its own silent
        # failure mode. If the re-observe came back landing.file_invalid
        # (below min_long_edge_px, wrong format, unreadable, unknown color
        # space) instead of landing.file_observed, the draft row is now
        # gone AND the landing row is 'invalid' -- worse off than before
        # the reset, not better. Surface that loudly instead of returning
        # a success report.
        valid_ids = {
            r["file_id"]
            for r in conn.execute(
                "SELECT file_id FROM proj_landing_files WHERE user_id=? AND status='valid'",
                (user_id,),
            ).fetchall()
        }
        still_broken = landing_file_ids - valid_ids
        if still_broken:
            raise ResetIncomplete(
                f"listings reset: re-observe failed for {sorted(still_broken)} -- "
                "check the file(s) still meet landing validation (format/resolution/"
                "color space) before retrying"
            )

    return report
