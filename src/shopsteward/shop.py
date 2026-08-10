"""Winners-folder orchestration: scan the landing folder, run vision-for-copy,
composite mockups, and build+push listing drafts -- the digital shop-build in
one call (PRD's "everything between the gates runs unattended" applied to the
manual-winners path). All live steps are gated + default off.

Lives at the top level, NOT under shopsteward.pipeline: it composes
shopsteward.pipeline and shopsteward.mockups, and the import-linter contract
"mockups is imported by no lower layer" (pyproject.toml) forbids
shopsteward.pipeline itself from importing shopsteward.mockups.
"""

import sqlite3
from pathlib import Path

from shopsteward.mockups.jobs import run_mockups
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.landing import scan_landing
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
from shopsteward.pipeline.listings.pod.factory import build_print_file_host
from shopsteward.pipeline.listings.vision_copy import run_vision_copy
from shopsteward.pipeline.live_gate import (
    live_copy_error,
    live_copy_open,
    live_etsy_write_error,
    live_etsy_write_open,
    live_printfile_error,
    live_printfile_open,
    live_vision_error,
    live_vision_open,
)
from shopsteward.pipeline.llm_ledger import current_month_prefix
from shopsteward.pipeline.vision_factory import build_vision_adapter


class LiveGateClosedError(RuntimeError):
    """Raised when a --live-* flag is set but its operator gate is closed."""


def run_shop_build(
    conn: sqlite3.Connection,
    user_id: int,
    folder: Path,
    *,
    live_vision: bool = False,
    live_copy: bool = False,
    live_etsy_write: bool = False,
    live_printfile: bool = False,
    regenerate: bool = False,
) -> dict:
    tuning.seed(conn, user_id, TUNING_PROFILE_PATH)
    profile = tuning.get_profile(conn, user_id)

    # Refuse up front (before any scan or spend) if a --live-* flag is set
    # but its gate is closed, mirroring the CLI-level checks in
    # listings/cli.py and cli.py::sync so a half-run never happens.
    if live_vision and not live_vision_open(profile.vision.provider):
        raise LiveGateClosedError(live_vision_error(profile.vision.provider))
    if live_copy and not live_copy_open():
        raise LiveGateClosedError(live_copy_error())
    if live_etsy_write and not live_etsy_write_open():
        raise LiveGateClosedError(live_etsy_write_error())
    if live_printfile and not live_printfile_open():
        raise LiveGateClosedError(live_printfile_error())

    landing = scan_landing(conn, user_id, folder)

    vision_adapter = build_vision_adapter(profile, live=live_vision)
    vision = run_vision_copy(
        conn,
        user_id,
        adapter=vision_adapter,
        model=profile.vision.triage_model,
        soft_cap_usd=profile.vision.monthly_soft_cap_usd,
        month_prefix=current_month_prefix(),
        regenerate=regenerate,
    )

    # Composite mockups for every eligible landing winner -- build_drafts
    # skips any winner without a completed mockup set
    # (drafts._completed_mockup_set reads proj_mockup_sets/proj_mockups).
    mockups = run_mockups(conn, user_id)

    drafts = build_drafts(
        conn,
        user_id,
        live_copy=live_copy,
        live_etsy_write=live_etsy_write,
    )

    # Costed physical POD drafts (variant selection + pricing + print-file
    # hosting, design §13 slice 1-2) for every eligible winner -- stops at
    # print_file_hosted; provider create/Etsy push/enrichment is Phase C.
    host = build_print_file_host(live=live_printfile)
    pod = build_pod_drafts(conn, user_id, print_file_host=host)

    return {
        "observed": landing.observed,
        "matched": landing.matched,
        "manual_drops": landing.manual_drops,
        "scored": vision["scored"],
        "vision_skipped": vision["skipped"],
        "vision_failed": vision["failed"],
        "vision_cap_hit": vision["cap_hit"],
        "mockup_sets": mockups.sets_completed,
        "mockups_written": mockups.mockups_written,
        "drafts": drafts.drafts_built,
        "pushed": drafts.pushed,
        "pod_drafts": pod.drafts_built,
        "pod_variants_priced": pod.variants_priced,
        "pod_print_files_hosted": pod.print_files_hosted,
        "pod_skipped": pod.pod_skipped,
    }
