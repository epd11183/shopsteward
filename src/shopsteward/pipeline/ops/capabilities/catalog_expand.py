"""`listing.catalog_expand` -- T11 (2026-08-25 design doc, "Design: paced
digital listings from the photo archive"): the operator's own /autoplan gate
answer, verbatim -- "a new capability proposing paced digital listings from
your archive, Gate-3 approval per listing, fees within the $20 cap" (publish
authority later moved to the read-back gate, see below). Policy:
VERIFIED -- docs/policy/2026-08-11-autonomy-platform-policy.md entry E16
(PERMITTED; listing our own original work is the core permitted seller act,
mechanically identical to E5 gap-fill), subject to E16's three accuracy
conditions: (1) the photograph is ours, never AI-generated, and AI never adds to it
(operator-directed distraction removal is allowed, amended 2026-11-22) --
the only image transform anywhere in this chain is `images._load_sellable`'s
deterministic max-quality sRGB JPEG re-encode (plus deterministic ratio
crops for multi-file digital listings): no upscale, no fill, no generative
step, ever; (2) the advertised print sizes must be
deliverable by the source file -- enforced by `min_long_edge_px` (design §6);
(3) AI-assisted copy carries the existing disclosure line -- the copy stage
(`copy.py`) is unchanged.

**Nothing new needed to be built for the listing itself** (design §0): the
M5a chain already does per-photo work end to end, and both of its stages
already accept a `photo_id` filter. `expand_one()` below is exactly that
four-call chain --

    adopt.ingest_one_file(path)   -> landing row + file_id
    archive.archive_master(...)   -> reprintable later, free
    run_mockups(photo_id=...)     -> listing images
    build_drafts(photo_id=...)    -> draft built AND pushed to Etsy as a
                                      DRAFT -> appears in gate3.queue()

-- a plain function, not a method, so a future CLI backfill can call it
directly without going through the autonomy chassis at all (design §2C
synthesis). `execute()` below wraps it with the read-back gate + publish.

**2026-11-23 operator decision (explicit): "full auto + gate".** The old
`max_tier = Tier.PROPOSE` pin and Gate-3-per-listing approval are gone:
`max_tier = Tier.AUTO` (config `autonomy.tier_floor` starts it at AUTO).
`execute()` runs `expand_one()`, then the read-back gate
(`pipeline/listings/readback.py`: structural checks read back from Etsy plus a
vision copy-vs-photo check). Pass -> `gate3.publish` (still the sole
`publish_listing` call site). Fail -> the draft stays a draft, the
`listingdraft.readback` event carries the problems, and the brief lists it
under NEEDS YOU. Pace stays `max_new_per_week`.

**Gate failure counts as EXECUTED, not refused.** By the time the gate runs,
`expand_one` has landed the file, so (a) a retry of a "refused" action would
fail `_candidates()` re-validation and terminalize as `StaleTargetError`, and
(b) pace counts `action.executed`: a refused failure would push another real
draft to Etsy every run. Executed (cost $0, `after.published=False`) burns one
pace slot -- real work happened -- and the landed file is never re-proposed.

**Undo** deactivates the published listing (`update_listing_state(...,
"inactive")`); the $0.20 fee is not refunded. A draft that was never published
has nothing to undo.

**`precondition_ok`** is the control preventing the worst realistic failure
mode here: fixture copy, or an unverifiable copy check, reaching a REAL Etsy
listing (design §12.6). It requires BOTH `live_copy` (live_gate.live_copy_open())
and `live_check` (the live copy checker is available). Independently, the
fixture checker never approves, so even a direct `execute()` cannot publish
unverified copy.

**`_candidates()` is the ONE grounding function shared by propose()/
materialize()/execute()** (M8b slice-2 planner-safety contract, every
capability here follows it), per design §3's steps:

1. `{}` if disabled or the folder is missing.
2. Scan the folder (`adopt.scan_local_files` -- sorted, JPEG/TIFF).
3. Skip anything already a landing file (`landing._known_file_ids`) --
   permanent execution idempotency for free (`adopt`'s own trick).
4. Skip anything with a prior `action.rejected` for THIS capability
   (`adopt._revoked` precedent) -- **load-bearing** (design §7): for every
   OTHER capability here, rejection means "the agent proposed something
   wrong"; here, "no, not that photo" is the expected routine answer, and
   without this skip the SAME file would be re-proposed tomorrow (action_id
   embeds the day, `_TERMINAL` is per-action_id, not per-target). Moving the
   file out of the folder is the documented "I changed my mind later" path.
5. Format + resolution bar (`landing._validate`, reused, never
   reimplemented) -- `min_long_edge_px` defaults to 6000, not the landing
   floor's 3000 (design §6.2 / E16 condition 2: `whatyougot` copy advertises
   16x20 at 300 DPI, which needs a 6000px long edge).
6. Near-duplicate guard: pHash against other candidates this run and every
   already-registered landing file, both local, zero network.

Sorted by path (already true of step 2's output) -- deterministic ordering,
no truncation.

**Pace is deliberately NOT step 7 here anymore** (H1, guardrail review
2026-08-25, third instance of this failure class): this function doubles as
`execute()`'s re-validation predicate, so a pace-only exclusion baked in
here would be indistinguishable from genuine staleness and would raise
`ValueError` -> `runner._execute_and_record` -> a TERMINAL `action.failed`,
permanently blocking any future real approval of that action_id -- exactly
the incident this chassis's own `LIVE_GATED_CAPABILITIES` docstring names
("burned the operator on 2026-08-24"), reintroduced through a new door.
Pace is now enforced in TWO places, each for a different reason, neither of
which is this function:
- `governor.govern()` enforces `cfg.catalog_expansion.max_new_per_week`
  as a `RefusalReason.PACE` REFUSAL (see governor.py's own comment) --
  the actual safety boundary. A refusal never terminalizes; the action
  stays "proposed" and is approvable again once the week's pace resets.
- `propose()` below still TRUNCATES how many brand-new proposals it mints
  per call to the same remaining-this-week count, purely so `ops run`
  doesn't flood the NEEDS-YOU queue with next month's candidates all at
  once -- minting fewer proposals is not a terminalizing act (design §5
  originally called the governor route "not a new governor concept"; the
  review reversed that call, and this module's docstring is corrected to
  match).

**Cost accounting** (design §3): Etsy charges the $0.20 at publish --
`execute()` books `cost_usd` 0 and `governor.month_spend()` counts the fee
once, from the `gate3.published` event. `estimated_cost_usd` still carries the
fee so the governor's pre-execution budget check reserves it. Drafts that fail
the gate are never published, so they are never charged. Copy-check LLM spend
goes to the `llm.call` ledger, not the $20 cap.

**Holdout: no change needed, by construction.**
`governor._pin_event_dates` does `int(target_id)` and returns `[]` on
`ValueError`; `target_id` here is a sha256 file_id, never int-parseable, so
the pin/seo holdout can never fire for this capability. Documented here so
nobody "fixes" it later."""

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from PIL import Image

from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.adapters.vision.interface import CopyChecker
from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.jobs import run_mockups
from shopsteward.pipeline import tuning
from shopsteward.pipeline.listings import adopt, archive, asset_store_config, gate3
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.photo_match import hamming_distance, phash_bytes
from shopsteward.pipeline.listings.readback import run_readback
from shopsteward.pipeline.ops.config import ops_config_hash
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id

_CAPABILITY = "listing.catalog_expand"
# The only two formats `landing._SUFFIX_FORMATS` ever maps a scanned file to
# (JPEG/TIFF) -- landing already allows both (tuning_profile.json's
# landing.allowed_formats). `_candidates()` is read-only (Capability
# protocol), so this is a plain constant rather than a tuning-profile read
# that could raise on an unseeded profile.
_ALLOWED_FORMATS = ("JPEG", "TIFF")


def expand_one(
    conn: sqlite3.Connection,
    user_id: int,
    path: Path,
    *,
    adapter: EtsyWriteAdapter,
    live_copy: bool,
) -> str:
    """The four-call chain (module docstring / design §0/§2C). Returns the
    resulting `draft_id`. Raises if the file fails landing validation (never
    silently skips -- a caller that got this far already decided this photo
    should become a listing) or if, unexpectedly, the build chain completes
    with no draft row for it."""
    from shopsteward.pipeline.config import TUNING_PROFILE_PATH
    from shopsteward.pipeline.landing import _SUFFIX_FORMATS

    # adopt.adopt_one's own precedent -- ingest_one_file() reads
    # tuning.get_profile() (landing.allowed_formats/min_long_edge_px), which
    # raises KeyError if nothing has been seeded yet for this user.
    tuning.seed(conn, user_id, TUNING_PROFILE_PATH)
    file_id = adopt.ingest_one_file(conn, user_id, path)
    if file_id is None:
        # StaleTargetError (H2b, guardrail review 2026-08-25): genuine
        # per-target staleness -> the runner terminalizes this one.
        raise StaleTargetError(f"{path}: failed landing validation -- cannot expand")

    photo_id = f"file-{file_id[:12]}"
    asset_store_config.seed(conn, user_id)
    from shopsteward.pipeline.listings.projections import rebuild_listings

    rebuild_listings(conn)  # cli.py's `listings adopt` precedent -- proj_asset_store_config
    asset_cfg = asset_store_config.get_asset_store_config(conn, user_id)
    fmt = _SUFFIX_FORMATS.get(path.suffix.lower(), "JPEG")
    with Image.open(path) as img:
        width, height = img.size
    archive.archive_master(
        conn,
        user_id,
        asset_cfg,
        photo_id=photo_id,
        source_landing_file_id=file_id,
        path=str(path),
        format=fmt,
        width=width,
        height=height,
    )

    run_mockups(conn, user_id, photo_id=photo_id)
    report = build_drafts(
        conn, user_id, photo_id=photo_id, live_copy=live_copy, etsy_adapter=adapter
    )

    row = conn.execute(
        "SELECT draft_id, title FROM proj_listing_drafts WHERE user_id=? AND landing_file_id=?",
        (user_id, file_id),
    ).fetchone()
    # A skipped draft (stale "what you get" panel) or one whose generated copy
    # was refused (undelivered size / refusal cap) says WHY instead of a bare
    # "no draft was built" -- these are fixed by re-rendering / operator review,
    # not by retrying blindly.
    if row is not None and row["title"] is None:
        from shopsteward.pipeline.listings.copy import MAX_COPY_REFUSALS, copy_refusals

        if copy_refusals(conn, user_id, row["draft_id"]) >= MAX_COPY_REFUSALS:
            # Terminal: the LLM is no longer called for this draft, so re-running
            # archive/mockups/build every cycle cannot help.
            raise StaleTargetError(
                f"{path}: copy refused {MAX_COPY_REFUSALS}x: needs operator review"
            )
    # No title for any reason (refused, or skipped for want of a vision score) is
    # retryable: never record a titleless draft as an executed expansion.
    if row is None or row["title"] is None:
        reasons = "; ".join(report.skip_reasons) or "no reason reported"
        raise RuntimeError(
            f"{path}: catalog-expand chain completed but no usable draft was built "
            f"(file_id={file_id!r}): {reasons}"
        )
    return row["draft_id"]


def _rejected_file_ids(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """target_id (file_id) with a prior `action.rejected` for THIS
    capability -- `adopt._revoked` precedent. Design §7, load-bearing: see
    module docstring."""
    capability_of: dict[str, str] = {}
    target_of: dict[str, str] = {}
    for e in read_all(conn, "action.proposed"):
        if e.user_id != user_id:
            continue
        capability_of[e.payload["action_id"]] = e.payload["capability"]
        target_of[e.payload["action_id"]] = str(e.payload["target_id"])

    out: set[str] = set()
    for e in read_all(conn, "action.rejected"):
        if e.user_id != user_id:
            continue
        action_id = e.payload["action_id"]
        if capability_of.get(action_id) != _CAPABILITY:
            continue
        target_id = target_of.get(action_id)
        if target_id is not None:
            out.add(target_id)
    return out


def _executed_this_iso_week(conn: sqlite3.Connection, user_id: int, today: date) -> int:
    """Count of THIS capability's `action.executed` rows in `today`'s ISO
    week -- the same history `governor._executed_this_iso_week` reads
    (that IS the safety-boundary pace check now, per H1 above); this copy
    is used only by `propose()` below to decide how many NEW proposals to
    mint this run, never to gate `_candidates()`'s eligibility set."""
    from shopsteward.pipeline.ops.timeutil import parse_ts

    capability_of: dict[str, str] = {}
    for e in read_all(conn, "action.proposed"):
        if e.user_id == user_id:
            capability_of[e.payload["action_id"]] = e.payload["capability"]

    year, week, _ = today.isocalendar()
    count = 0
    for e in read_all(conn, "action.executed"):
        if e.user_id != user_id or not e.created_at:
            continue
        if capability_of.get(e.payload["action_id"]) != _CAPABILITY:
            continue
        day = parse_ts(e.created_at).date()
        if day.isocalendar()[:2] == (year, week):
            count += 1
    return count


# ponytail: rehashes every already-registered landing file on every
# propose() call -- O(landing catalog size), no caching. Fine at a few dozen
# listings (this shop's scale); upgrade to a stored-hash projection column
# if the archive/landing catalog ever grows enough for this to show up in
# `ops run`'s wall-clock time.
def _existing_landing_hashes(conn: sqlite3.Connection, user_id: int) -> list[int]:
    try:
        rows = conn.execute(
            "SELECT path FROM proj_landing_files WHERE user_id=?", (user_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        # proj_landing_files (core pipeline) hasn't been rebuilt yet in this
        # conn -- governor._active_listing_count precedent: no catalog to
        # protect against, not a crash.
        return []
    hashes: list[int] = []
    for row in rows:
        try:
            hashes.append(phash_bytes(Path(row["path"]).read_bytes()))
        except (OSError, ValueError):
            continue  # unreadable/missing local file -- never fatal to a propose() scan
    return hashes


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
) -> dict[str, ProposedAction]:
    """file_id -> the ProposedAction propose() would build for it -- see
    module docstring for design §3's steps (pace is deliberately excluded,
    H1 2026-08-25)."""
    ce = cfg.catalog_expansion
    out: dict[str, ProposedAction] = {}
    if not ce.enabled:
        return out

    folder = Path(ce.source_folder)
    if not folder.is_dir():
        return out

    today_date = datetime.now(UTC).date()

    from shopsteward.pipeline.landing import (
        _SUFFIX_FORMATS,
        _known_file_ids,
        _sha256_file,
        _validate,
    )

    known_ids = _known_file_ids(conn, user_id)
    rejected_ids = _rejected_file_ids(conn, user_id)
    existing_hashes = _existing_landing_hashes(conn, user_id)
    candidate_hashes: list[int] = []

    today = today_date.isoformat()
    cfg_hash = ops_config_hash(cfg)
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()

    for path in adopt.scan_local_files(folder, ce.recursive):
        file_id = _sha256_file(path)
        if file_id in known_ids or file_id in rejected_ids:
            continue

        fmt = _SUFFIX_FORMATS.get(path.suffix.lower())
        if fmt is None:
            continue
        result = _validate(
            path, fmt=fmt, allowed_formats=_ALLOWED_FORMATS, min_long_edge_px=ce.min_long_edge_px
        )
        if "reason" in result:
            continue

        try:
            file_hash = phash_bytes(path.read_bytes())
        except ValueError:
            continue  # undecodable image -- never a candidate
        if any(hamming_distance(file_hash, h) <= ce.dedup_max_distance for h in existing_hashes):
            continue  # already in the catalog under a different name/crop
        if any(hamming_distance(file_hash, h) <= ce.dedup_max_distance for h in candidate_hashes):
            continue  # duplicate of another candidate already picked THIS run
        candidate_hashes.append(file_hash)

        raw = "|".join((file_id, str(path)))
        inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
        action_id = compute_action_id(_CAPABILITY, file_id, inputs_hash, cfg_hash, today)
        out[file_id] = ProposedAction(
            action_id=action_id,
            capability=_CAPABILITY,
            target_type="archive_photo",
            target_id=file_id,
            tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
            reason=(
                f"archive photo {path.name} -- new digital listing, auto-published if "
                f"it passes the read-back gate (${ce.listing_fee_usd:.2f} at publish, "
                "non-refundable)."
            ),
            inputs_hash=inputs_hash,
            estimated_cost_usd=ce.listing_fee_usd,
            undo_available=True,
            expires_at=expires_at,
            params={"path": str(path)},
        )
    return out


class ListingCatalogExpand:
    key = _CAPABILITY
    # 2026-11-23 operator decision: AUTO behind the read-back gate; the undo
    # below (deactivate) satisfies registry.py invariant 1.
    max_tier = Tier.AUTO
    policy_verified = True  # E16 -- docs/policy/2026-08-11-autonomy-platform-policy.md

    def __init__(
        self,
        adapter: EtsyWriteAdapter,
        *,
        live_copy: bool,
        checker: CopyChecker,
        live_check: bool,
    ) -> None:
        self._adapter = adapter
        self._live_copy = live_copy
        self._checker = checker
        # The control preventing fixture copy / an unverifiable copy check from
        # reaching a real Etsy listing (module docstring) -- governor.py's own
        # `getattr(cap, "precondition_ok", True)` -> RefusalReason.PRECONDITION.
        self.precondition_ok = live_copy and live_check

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        # H1: pace is NOT eligibility (see module docstring) -- this only
        # limits how many BRAND-NEW proposals get minted this run, purely
        # to avoid flooding the NEEDS-YOU queue; it never excludes an
        # already-eligible candidate from `_candidates()` itself, and it
        # never fails an approval. The real safety-boundary pace check is
        # `governor.govern()`'s `RefusalReason.PACE`.
        candidates = list(_candidates(conn, user_id, cfg).values())
        today_date = datetime.now(UTC).date()
        remaining = cfg.catalog_expansion.max_new_per_week - _executed_this_iso_week(
            conn, user_id, today_date
        )
        if remaining <= 0:
            return []
        return candidates[:remaining]

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        return _candidates(conn, user_id, cfg).get(intent.target_id)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        from shopsteward.pipeline.ops.config import get_ops_config

        cfg = get_ops_config(conn, user_id)
        if action.target_id not in _candidates(conn, user_id, cfg):
            # Re-validate at execute time (renew.py precedent): never spend
            # on a decision that changed since propose() (already landed by
            # someone else, rejected in the meantime, file removed, etc).
            # H1: deliberately NEVER a pace reason -- `_candidates()` has no
            # pace gating (see module docstring); an over-pace approval is
            # refused earlier, in `governor.govern()`, before execute() is
            # ever called. StaleTargetError (H2b, guardrail review
            # 2026-08-25): genuine per-target staleness -> the runner
            # terminalizes this one.
            raise StaleTargetError(
                f"archive photo {action.target_id!r}: no longer eligible -- refusing to expand"
            )

        path = Path(str(action.params["path"]))
        draft_id = expand_one(conn, user_id, path, adapter=self._adapter, live_copy=self._live_copy)

        # From here a draft exists on Etsy: nothing below may raise (the file
        # is landed, so a raise could never be retried -- module docstring).
        model = tuning.get_profile(conn, user_id).vision.rescore_model
        gate = run_readback(
            conn, user_id, draft_id, adapter=self._adapter, checker=self._checker, model=model
        )
        problems = list(gate.problems)
        published = False
        if gate.ok:
            card = gate3.publish(conn, user_id, draft_id, self._adapter)  # sole publish site
            published = card.state == "published"
            if not published:
                problems = [f"publish failed: {card.retry_error or card.state}"]
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="listingdraft.readback",
                        payload={"draft_id": draft_id, "ok": False, "problems": problems},
                    ),
                )
        row = conn.execute(
            "SELECT etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
            (user_id, draft_id),
        ).fetchone()
        return ExecutionResult(
            before={"draft_id": None},
            after={
                "draft_id": draft_id,
                "etsy_listing_id": row["etsy_listing_id"] if row else None,
                "published": published,
                "problems": problems,
            },
            # ponytail: executed cost is 0 -- the fee is booked once, at
            # gate3.published (governor.month_spend_parts); estimated_cost_usd
            # reserves it beforehand. Unpublished drafts are never charged.
            cost_usd=0.0,
            duration_ms=0,
        )

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        """Deactivate the published listing (fee not refunded); a draft that
        was never published has nothing to undo."""
        after: dict = {}
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                after = e.payload.get("after") or {}
        if after.get("published") and after.get("etsy_listing_id") is not None:
            self._adapter.update_listing_state(int(after["etsy_listing_id"]), "inactive")

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return action.estimated_cost_usd
