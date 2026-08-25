"""`listing.catalog_expand` -- T11 (2026-08-25 design doc, "Design: paced
digital listings from the photo archive"): the operator's own /autoplan gate
answer, verbatim -- "a new capability proposing paced digital listings from
your archive, Gate-3 approval per listing, fees within the $20 cap." Policy:
VERIFIED -- docs/policy/2026-08-11-autonomy-platform-policy.md entry E16
(PERMITTED; listing our own original work is the core permitted seller act,
mechanically identical to E5 gap-fill -- ends in an unpublished Etsy draft,
Gate 3 is the publish authority), subject to E16's three accuracy
conditions: (1) the photograph is ours and never AI-generated/AI-edited --
the only image transform anywhere in this chain is `images._load_sellable`'s
deterministic max-quality sRGB JPEG re-encode: no resize, no upscale, no
fill, no generative step, ever; (2) the advertised print sizes must be
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
synthesis). `execute()` below is a two-line wrapper around it.

**`max_tier = Tier.PROPOSE` is a Python pin, never promotable** -- same class
as `gapfill_reprint`: it creates catalog and commits real money, and Etsy has
no "un-list without a trace." `registry.register()`'s invariant 2 plus
`runner._maybe_promote`'s `int(to_tier) < int(cap.max_tier)` check mean no
config path can ever raise it. **`undo = None`** -- allowed because max_tier
is not below PROPOSE (registry.py invariant 1); there is nothing to reverse
programmatically anyway (gapfill.py precedent) -- the reversal is the
operator declining to publish at Gate 3.

**`precondition_ok`** is the single control preventing the worst realistic
failure mode here: a `FixtureCopyAdapter` run putting canned fixture copy on
a REAL Etsy listing (design §12.6). It is set once at construction from the
`live_copy` bool the caller (cli.py) resolves via `live_gate.live_copy_open()`
-- not re-evaluated per governor call, matching how `execute()`/`expand_one`
thread the same flag through to `drafts.build_drafts(live_copy=...)`.

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

**Cost accounting -- the honest version** (design §3): Etsy actually charges
the $0.20 at Gate-3 publish, outside this chassis -- `execute()` does not
actually spend. `estimated_cost_usd`/`ExecutionResult.cost_usd` still carry
`0.20` anyway, a deliberate OVER-reserve, because it is the only point the
governor's `month_spend()` can see. A declined draft is over-counted, which
fails safe; under-counting would let listings publish against a governor
that saw $0 spent.

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
from shopsteward.core.events import read_all
from shopsteward.mockups.jobs import run_mockups
from shopsteward.pipeline.listings import adopt, archive, asset_store_config
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.photo_match import hamming_distance, phash_bytes
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
    from shopsteward.pipeline import tuning
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
    build_drafts(conn, user_id, photo_id=photo_id, live_copy=live_copy, etsy_adapter=adapter)

    row = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND landing_file_id=?",
        (user_id, file_id),
    ).fetchone()
    if row is None:  # pragma: no cover - defensive, the chain above always builds one
        raise RuntimeError(
            f"{path}: catalog-expand chain completed but no draft was built (file_id={file_id!r})"
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
                f"archive photo {path.name} -- new digital listing "
                f"(${ce.listing_fee_usd:.2f} at Gate-3 publish, non-refundable)."
            ),
            inputs_hash=inputs_hash,
            estimated_cost_usd=ce.listing_fee_usd,
            undo_available=False,
            expires_at=expires_at,
            params={"path": str(path)},
        )
    return out


class ListingCatalogExpand:
    key = _CAPABILITY
    # T11 hard ceiling -- NEVER promotable (creates catalog + real money;
    # registry.py's invariant 2 enforces there is no config path that can
    # raise this Python ceiling). See module docstring.
    max_tier = Tier.PROPOSE
    policy_verified = True  # E16 -- docs/policy/2026-08-11-autonomy-platform-policy.md
    # No undo path: allowed because max_tier is not below PROPOSE (registry.py
    # invariant 1) -- reversal is Gate 3 (decline to publish), same as
    # gapfill_reprint (draft #16 precedent).
    undo = None

    def __init__(self, adapter: EtsyWriteAdapter, *, live_copy: bool) -> None:
        self._adapter = adapter
        self._live_copy = live_copy
        # The single control preventing fixture copy from landing on a real
        # Etsy listing (module docstring) -- governor.py's own
        # `getattr(cap, "precondition_ok", True)` -> RefusalReason.PRECONDITION.
        self.precondition_ok = live_copy

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
        return ExecutionResult(
            before={"draft_id": None},
            after={"draft_id": draft_id},
            cost_usd=action.estimated_cost_usd,
            duration_ms=0,
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return action.estimated_cost_usd
