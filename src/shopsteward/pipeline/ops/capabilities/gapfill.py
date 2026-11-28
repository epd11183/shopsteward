"""`listing.gapfill_reprint` -- autonomous gap-fill (design
docs/designs/2026-11-27-gapfill-reprint-autonomous.md): Claude reprints a
PROVEN best-seller into a NEW POD format and, behind a physical read-back gate,
publishes it. `max_tier = Tier.AUTO` (operator, 2026-11-27); config
`autonomy.tier_floor` starts it at AUTO.

The chain (steps 0-4 raise = a non-terminal refusal that counts as an attempt;
after step 5 nothing raises):

0. re-validate the target against `_candidates()`;
1. local preflight, no network: the print master resolves (archive fallback)
   and the mockup set exists (built via the mockups hook if not);
2. `build_pod_reprint` with the live print-file host (an existing own draft is
   resumed);
3. `link_pod_drafts(draft_ids={draft})` -- the filter is the point: unfiltered
   it would create a Gelato product for EVERY unlinked POD draft;
4. `enrich_pod_drafts(draft_ids={draft})` (POD copy, no digital panel);
5. auto-renew off if Etsy shows it on (a listing field, never a SKU/variation);
6. `run_physical_readback`; pass -> `gate3.publish` (the sole publish site).
   Fail -> the draft stays a draft under NEEDS YOU. Either way the action is
   `executed` with `after={draft_id, etsy_listing_id, provider_product_id,
   published, problems}`.

POD-first: provider SKUs, variations and price are read, never written.

**Money.** Gelato create costs nothing up front (Gelato is on the free plan);
the Etsy $0.20 listing fee is charged at publish and booked by
`governor.month_spend_parts` from `gate3.published`. `estimated_cost_usd` is
that fee so the governor reserves it; `ExecutionResult.cost_usd` is 0.

**Pace unit = one new Gelato product** (first `provider_create_attempted`),
not one execution -- see `pace_exceeded()`, called by `governor.govern()`.

**Own drafts only.** A draft is resumed and auto-published only if it carries
a `listingdraft.gapfill_claimed` event. Drafts built by older offline,
operator-approved gapfill runs and drafts built by `shop build` are
operator-owned: never resumed, never auto-published. `link_pod_drafts` and
`enrich_pod_drafts` skip claimed drafts unless the caller names them in
`draft_ids`, so `shop build`/`pod link`/`pod enrich` never advance them: only
this capability does, under its pace.

**Ambiguous create.** A create attempt with no `provider_created` is never
retried (the live create has no idempotency key): the draft is closed with a
`listingdraft.readback` failure telling the operator to check the Gelato
dashboard.

**Undo** deactivates the published listing (fee not refunded). The Gelato
product is left in place: `delete_product` removes the connected Etsy listing
and its history. `provider_product_id` stays in `after` for manual cleanup.

`policy_verified` is True: policy row E17 (docs/policy/
2026-08-11-autonomy-platform-policy.md, 2026-11-27)."""

import contextlib
import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta

from shopsteward.adapters.copy.interface import CopyAdapter
from shopsteward.adapters.etsy.interface import EtsyAdapter, EtsyWriteAdapter, EtsyWriteError
from shopsteward.adapters.etsy.models import EtsyListingUpdate
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.adapters.pod.interface import PodAdapter
from shopsteward.adapters.printfile.interface import PrintFileHost
from shopsteward.adapters.vision.interface import CopyChecker
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings import gate3
from shopsteward.pipeline.listings.drafts import _completed_mockup_set, _mockup_projections_ready
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod import printfile
from shopsteward.pipeline.listings.pod.build import (
    _archived_row_for_photo,
    build_pod_reprint,
    reprint_eligible_types,
)
from shopsteward.pipeline.listings.pod.enrich import enrich_pod_drafts
from shopsteward.pipeline.listings.pod.provider import claimed_by_gapfill, link_pod_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.readback import run_physical_readback
from shopsteward.pipeline.listings.source_assets import resolve_source
from shopsteward.pipeline.mockup_hooks import get as mockup_hook
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.capabilities.catalog_expand import _failed_attempts
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id
from shopsteward.pipeline.ops.timeutil import parse_ts

_CAPABILITY = "listing.gapfill_reprint"
ATTEMPT_TAG = "gapfill"


def _draft_exists(conn: sqlite3.Connection, user_id: int, draft_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND draft_id=? LIMIT 1",
            (user_id, draft_id),
        ).fetchone()
        is not None
    )


def compute_draft_id(photo_id: str, pod_cfg_hash: str, provider: str, product_type: str) -> str:
    """The same id `build_pod_reprint` derives (photo|config hash|provider|type)."""
    raw = f"{photo_id}|{pod_cfg_hash}|{provider}|{product_type}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _has_mockups(conn: sqlite3.Connection, user_id: int, file_id: str) -> bool:
    return (
        _mockup_projections_ready(conn)
        and _completed_mockup_set(conn, user_id, file_id) is not None
    )


def _provider_by_type(pod_cfg: pod_config.PodConfig) -> dict[str, str]:
    # ponytail: assumes a 1:1 product_type -> provider catalog (true today,
    # every product_type in pod.json has exactly one provider). Under a
    # many-providers catalog this dedup-only pick could differ from the
    # provider build_pod_reprint's own variant selection uses -- at worst a
    # noisy proposal that fails loudly at execute(), never a duplicate draft.
    out: dict[str, str] = {}
    for provider, provider_cat in pod_cfg.catalog.items():
        for product_type in provider_cat.products:
            out.setdefault(product_type, provider)
    return out


def action_draft_id(conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> str | None:
    """The draft this action works on: `params.draft_id` when minted by the
    autonomous version, else recomputed (proposals stored before it existed)."""
    stored = action.params.get("draft_id")
    if stored:
        return str(stored)
    photo_id, product_type = action.params.get("photo_id"), action.params.get("product_type")
    if not photo_id or not product_type:
        return None
    pod_cfg = pod_config.get_pod_config(conn, user_id)
    provider = _provider_by_type(pod_cfg).get(str(product_type))
    if provider is None:
        return None
    return compute_draft_id(
        str(photo_id), pod_config.pod_config_hash(pod_cfg), provider, str(product_type)
    )


# -- event-derived state per draft ------------------------------------------------


def _draft_ids(conn: sqlite3.Connection, user_id: int, event_type: str) -> set[str]:
    return {
        e.payload["draft_id"]
        for e in read_all(conn, event_type)
        if e.user_id == user_id and e.payload.get("draft_id")
    }


def _claimed(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Drafts this capability built, by explicit claim only. Older offline
    runs' drafts are operator-owned (module docstring)."""
    return claimed_by_gapfill(conn, user_id)


def _finished(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Published, or the read-back gate ran (pass-then-publish-failed and every
    gate failure append a `listingdraft.readback`)."""
    return _draft_ids(conn, user_id, "gate3.published") | _draft_ids(
        conn, user_id, "listingdraft.readback"
    )


def _unconfirmed(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Create attempted, never confirmed: never re-created."""
    return _draft_ids(conn, user_id, "listingdraft.provider_create_attempted") - _draft_ids(
        conn, user_id, "listingdraft.provider_created"
    )


def _first_attempts(conn: sqlite3.Connection, user_id: int) -> dict[str, date]:
    """draft_id -> date of its first gapfill-tagged `provider_create_attempted`."""
    out: dict[str, date] = {}
    for e in read_all(conn, "listingdraft.provider_create_attempted"):
        if e.user_id != user_id or e.payload.get("tag") != ATTEMPT_TAG or not e.created_at:
            continue
        out.setdefault(e.payload["draft_id"], parse_ts(e.created_at).date())
    return out


def in_flight(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Own drafts whose Gelato product exists and whose chain has not finished."""
    return (
        set(_first_attempts(conn, user_id))
        & _draft_ids(conn, user_id, "listingdraft.provider_created")
        & _claimed(conn, user_id)
    ) - _finished(conn, user_id)


def pace_exceeded(
    conn: sqlite3.Connection,
    user_id: int,
    action: ProposedAction,
    cfg: OpsConfig,
    today: date,
) -> bool:
    """Governor PACE for a NEW Gelato product (design §4): this ISO week already
    has `max_per_week` first create attempts on gapfill drafts, or another
    gapfill draft is in flight. Resuming a draft that already attempted its
    create is never pace-refused."""
    draft_id = action_draft_id(conn, user_id, action)
    attempts = _first_attempts(conn, user_id)
    if draft_id is not None and draft_id in attempts:
        return False
    this_week = sum(1 for d in attempts.values() if d.isocalendar()[:2] == today.isocalendar()[:2])
    if this_week >= cfg.gapfill.max_per_week:
        return True
    return bool(in_flight(conn, user_id) - {draft_id})


# -- candidates --------------------------------------------------------------------


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
) -> dict[str, ProposedAction]:
    """`f"{listing_id}:{product_type}"` -> the ProposedAction propose() would
    build. The reprint ceiling: a PROVEN listing with an ARCHIVED source and a
    format the photo has no draft for -- or an own draft that is unfinished,
    unambiguous and under `max_attempts` failures (resumed). Pace is NOT
    here (this is also execute()'s re-validation predicate; a pace-only
    exclusion would terminalize a re-approvable action): `governor.govern()`
    owns it."""
    sellers = analytics.proven_listings(conn, user_id, cfg)
    if not sellers:
        return {}

    pod_cfg = pod_config.get_pod_config(conn, user_id)
    pod_cfg_hash = pod_config.pod_config_hash(pod_cfg)
    provider_by_type = _provider_by_type(pod_cfg)
    claimed = _claimed(conn, user_id)
    skip = _finished(conn, user_id) | _unconfirmed(conn, user_id)
    attempts = _failed_attempts(conn, user_id, _CAPABILITY)

    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()

    out: dict[str, ProposedAction] = {}
    for seller in sellers:
        source = resolve_source(conn, user_id, seller.listing_id)
        if source is None or not source.archived or source.photo_id is None:
            continue  # no archived source: never guess a reprint

        # Only formats the build step can actually build for THIS photo (a landscape
        # photo has no canvas_portrait: build_pod_reprint refuses it terminally).
        buildable = reprint_eligible_types(conn, user_id, source.photo_id, pod_cfg)
        for product_type, provider in provider_by_type.items():
            draft_id = compute_draft_id(source.photo_id, pod_cfg_hash, provider, product_type)
            # Never strand one of our own in-flight drafts if the eligibility rules move.
            if product_type not in buildable and draft_id not in claimed:
                continue
            if _draft_exists(conn, user_id, draft_id) and (
                draft_id not in claimed or draft_id in skip
            ):
                continue  # not ours, or finished/ambiguous: never re-proposed
            target_id = f"{seller.listing_id}:{product_type}"
            if attempts.get(target_id, 0) >= cfg.gapfill.max_attempts:
                continue  # needs the operator (a NEEDS YOU line was written)

            raw = "|".join((source.photo_id, product_type, str(seller.units)))
            inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
            action_id = compute_action_id(_CAPABILITY, target_id, inputs_hash, pod_cfg_hash, today)
            trailing = (
                "reprint the proven winner."
                if seller.units > 0
                else "print a first physical copy -- rising views, still no sale."
            )
            fee = cfg.catalog_expansion.listing_fee_usd
            out[target_id] = ProposedAction(
                action_id=action_id,
                capability=_CAPABILITY,
                target_type="listing_reprint",
                target_id=target_id,
                tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
                reason=(
                    f"{analytics.proof_phrase(seller)}; no {product_type} yet -- {trailing} "
                    f"Auto-published if it passes the read-back gate (${fee:.2f} at publish, "
                    "non-refundable)."
                ),
                inputs_hash=inputs_hash,
                estimated_cost_usd=fee,
                undo_available=True,
                expires_at=expires_at,
                params={
                    "photo_id": source.photo_id,
                    "product_type": product_type,
                    "listing_id": seller.listing_id,
                    "draft_id": draft_id,
                },
            )
    # most units sold first (CMO), then target for determinism
    units = {s.listing_id: s.units for s in sellers}
    return dict(
        sorted(out.items(), key=lambda kv: (-units.get(int(kv[1].params["listing_id"]), 0), kv[0]))
    )


class ListingGapfillReprint:
    key = _CAPABILITY
    max_tier = Tier.AUTO  # operator 2026-11-27; the undo below satisfies invariant 1
    policy_verified = True  # policy row E17 (2026-11-27)

    def __init__(
        self,
        print_file_host: PrintFileHost,
        *,
        pod_adapter: PodAdapter | None = None,
        adapter: EtsyWriteAdapter | None = None,
        read_adapter: EtsyAdapter | None = None,
        copy_adapter: CopyAdapter | None = None,
        checker: CopyChecker | None = None,
        live_gelato: bool = False,
        live_printfile: bool = False,
        live_copy: bool = False,
        live_check: bool = False,
        live_etsy_read: bool = False,
        listing_fee_usd: float | None = None,
    ) -> None:
        self._host = print_file_host
        self._pod = pod_adapter
        self._adapter = adapter
        self._read = read_adapter
        self._copy = copy_adapter
        self._checker = checker
        self._live_copy = live_copy
        self._fee = listing_fee_usd
        # Every dependency present AND every live gate open (design §5): the
        # control stopping fixture copy, a fixture checker or an unreachable
        # Gelato/R2/Etsy-read from reaching a real listing.
        self.precondition_ok = (
            all(x is not None for x in (pod_adapter, adapter, read_adapter, copy_adapter, checker))
            and live_gelato
            and live_printfile
            and live_copy
            and live_check
            and live_etsy_read
        )

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return list(_candidates(conn, user_id, cfg).values())

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        return _candidates(conn, user_id, cfg).get(intent.target_id)

    # -- execute -----------------------------------------------------------------

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        from shopsteward.pipeline.ops.config import get_ops_config

        if (
            self._pod is None
            or self._adapter is None
            or self._read is None
            or self._copy is None
            or self._checker is None
        ):
            raise RuntimeError("gapfill_reprint is not wired for execution (offline construction)")
        pod_adapter, adapter, read_adapter, copy_adapter, checker = (
            self._pod,
            self._adapter,
            self._read,
            self._copy,
            self._checker,
        )

        cfg = get_ops_config(conn, user_id)
        # An attempted-but-unconfirmed create (a crash, or an error provider.py does
        # not catch) is never re-created and always gets its NEEDS YOU line, even
        # though the draft is no longer a candidate.
        unconfirmed_id = action_draft_id(conn, user_id, action)
        if (
            unconfirmed_id is not None
            and unconfirmed_id in _unconfirmed(conn, user_id) - _finished(conn, user_id)
            and unconfirmed_id in _claimed(conn, user_id)
        ):
            return self._close_unconfirmed(conn, user_id, unconfirmed_id)
        # Step 0: re-validate (renew.py precedent) -- genuine staleness terminalizes.
        if action.target_id not in _candidates(conn, user_id, cfg):
            raise StaleTargetError(
                f"gapfill target {action.target_id!r}: no longer eligible -- refusing to reprint"
            )
        photo_id = str(action.params["photo_id"])
        product_type = str(action.params["product_type"])
        draft_id = action_draft_id(conn, user_id, action)
        if draft_id is None:
            raise StaleTargetError(f"gapfill target {action.target_id!r}: no draft id")
        pod_cfg = pod_config.get_pod_config(conn, user_id)

        def fail(message: str) -> RuntimeError:
            """A retryable raise; the LAST allowed attempt also writes the
            NEEDS YOU line (a `listingdraft.readback` failure)."""
            tried = _failed_attempts(conn, user_id, _CAPABILITY).get(action.target_id, 0)
            if tried + 1 >= cfg.gapfill.max_attempts:
                self._close(conn, user_id, draft_id, [f"gapfill gave up: {message}"])
            return RuntimeError(message)

        try:
            self._preflight(conn, user_id, photo_id, pod_cfg)
            self._build(conn, user_id, action, photo_id, product_type, draft_id, pod_cfg)
            self._link(conn, user_id, draft_id, pod_adapter, pod_cfg)
        except StaleTargetError:
            raise  # genuine staleness terminalizes (a ValueError subclass: keep it first)
        except Exception as exc:  # noqa: BLE001 - any pre-publish failure is a counted, retryable refusal
            if draft_id in _unconfirmed(conn, user_id):
                return self._close_unconfirmed(conn, user_id, draft_id)
            raise fail(f"{type(exc).__name__}: {str(exc)[:300]}") from exc

        row = self._row(conn, user_id, draft_id)
        if row is None or row["etsy_listing_id"] is None:
            if draft_id in _unconfirmed(conn, user_id):
                return self._close_unconfirmed(conn, user_id, draft_id)
            raise fail("Gelato product not linked yet (will re-poll next run)")

        try:
            listing_cfg = listing_config.get_config(conn, user_id)
            enrich_pod_drafts(
                conn,
                user_id,
                etsy_adapter=adapter,
                copy_adapter=copy_adapter,
                cfg=listing_cfg,
                live=self._live_copy,
                draft_ids={draft_id},
            )
        except Exception as exc:  # noqa: BLE001 - any enrich failure is a counted, retryable refusal
            raise fail(f"enrich failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        row = self._row(conn, user_id, draft_id)
        if row is None or row["pod_status"] != "enriched":
            raise fail(f"draft not enriched (pod_status={row['pod_status'] if row else None})")

        # From here a real draft exists on Etsy: nothing below may raise. An
        # escape would leave an ok read-back as the latest event (draft dropped
        # from the candidates AND from NEEDS YOU), so every failure closes the
        # draft with a failed read-back instead.
        try:
            return self._finish(
                conn,
                user_id,
                draft_id,
                int(row["etsy_listing_id"]),
                pod_cfg,
                adapter,
                read_adapter,
                checker,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed: never raise past the first live write
            published = draft_id in _draft_ids(conn, user_id, "gate3.published")
            problems = [f"gapfill post-enrich step failed: {type(exc).__name__}: {str(exc)[:200]}"]
            if not published:
                self._close(conn, user_id, draft_id, problems)
            return self._result(conn, user_id, draft_id, published, problems)

    def _finish(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        draft_id: str,
        listing_id: int,
        pod_cfg: pod_config.PodConfig,
        adapter: EtsyWriteAdapter,
        read_adapter: EtsyAdapter,
        checker: CopyChecker,
    ) -> ExecutionResult:
        """Steps 5-6: auto-renew off, physical read-back, publish."""
        # step 5 (the gate re-reads should_auto_renew, so a failed write fails the gate)
        with contextlib.suppress(EtsyWriteError):
            if adapter.get_listing(listing_id).should_auto_renew:
                adapter.update_listing(listing_id, EtsyListingUpdate(should_auto_renew=False))

        model = tuning.get_profile(conn, user_id).vision.copy_check_model
        gate = run_physical_readback(
            conn,
            user_id,
            draft_id,
            adapter=adapter,
            read_adapter=read_adapter,
            checker=checker,
            model=model,
            pod_cfg=pod_cfg,
        )
        problems = list(gate.problems)
        published = False
        if gate.ok:
            try:
                card = gate3.publish(conn, user_id, draft_id, adapter)  # sole publish site
                published = card.state == "published"
                if not published:
                    problems = [f"publish failed: {card.retry_error or card.state}"]
            except ValueError as exc:  # check_publishable: not publishable
                problems = [f"publish failed: {str(exc)[:200]}"]
            if not published:
                self._close(conn, user_id, draft_id, problems)
        return self._result(conn, user_id, draft_id, published, problems)

    # -- steps ---------------------------------------------------------------------

    def _close_unconfirmed(
        self, conn: sqlite3.Connection, user_id: int, draft_id: str
    ) -> ExecutionResult:
        """Never re-create (ambiguous timeout, crash, or a failed create): the
        operator checks the Gelato dashboard."""
        problems = [
            f"Gelato create unconfirmed -- check the Gelato dashboard for draft "
            f"{draft_id}; never re-created"
        ]
        self._close(conn, user_id, draft_id, problems)
        return self._result(conn, user_id, draft_id, False, problems)

    @staticmethod
    def _row(conn: sqlite3.Connection, user_id: int, draft_id: str) -> sqlite3.Row | None:
        rebuild_listings(conn)
        return conn.execute(
            "SELECT etsy_listing_id, pod_status, provider_product_id FROM proj_listing_drafts "
            "WHERE user_id=? AND draft_id=?",
            (user_id, draft_id),
        ).fetchone()

    @staticmethod
    def _close(conn: sqlite3.Connection, user_id: int, draft_id: str, problems: list[str]) -> None:
        """Mark the draft finished and put it under NEEDS YOU (the brief reads
        the latest `listingdraft.readback`)."""
        append(
            conn,
            Event(
                user_id=user_id,
                type="listingdraft.readback",
                payload={"draft_id": draft_id, "ok": False, "problems": problems},
            ),
        )

    def _result(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        draft_id: str,
        published: bool,
        problems: list[str],
    ) -> ExecutionResult:
        row = self._row(conn, user_id, draft_id)
        return ExecutionResult(
            before={"draft_id": None},
            after={
                "draft_id": draft_id,
                "etsy_listing_id": row["etsy_listing_id"] if row else None,
                "provider_product_id": row["provider_product_id"] if row else None,
                "published": published,
                "problems": problems,
            },
            # ponytail: the $0.20 is booked once, at gate3.published
            # (governor.month_spend_parts); estimated_cost_usd reserves it.
            cost_usd=0.0,
            duration_ms=0,
        )

    def _preflight(
        self, conn: sqlite3.Connection, user_id: int, photo_id: str, pod_cfg: pod_config.PodConfig
    ) -> None:
        """Step 1, no network and no provider write: the print master resolves
        (archive fallback) and the mockup set exists."""
        tuning.seed(conn, user_id, TUNING_PROFILE_PATH)
        archived = _archived_row_for_photo(conn, user_id, photo_id, pod_cfg.print_file.prefer)
        if archived is None:
            raise LookupError(f"photo {photo_id!r} has no archived master")
        file_id = archived["source_landing_file_id"]
        printfile.resolve_print_source_path(conn, user_id, file_id, pod_cfg.print_file.prefer)
        if not _has_mockups(conn, user_id, file_id):
            mockup_hook("run_mockups")(conn, user_id, photo_id=photo_id, file_id=file_id)
            if not _has_mockups(conn, user_id, file_id):
                raise LookupError(f"no mockup set could be built for file {file_id[:12]}")

    def _build(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        action: ProposedAction,
        photo_id: str,
        product_type: str,
        draft_id: str,
        pod_cfg: pod_config.PodConfig,
    ) -> None:
        """Step 2. The claim is appended BEFORE the build so a crash between the
        two can never orphan an own draft; `_candidates` guarantees any draft
        already present at this point is already ours."""
        if draft_id not in _claimed(conn, user_id):
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="listingdraft.gapfill_claimed",
                    payload={"draft_id": draft_id, "target_id": action.target_id},
                ),
            )
        result = build_pod_reprint(
            conn, user_id, photo_id, product_type, print_file_host=self._host, pod_cfg=pod_cfg
        )
        if not result.built and result.reason != "already_exists":
            raise StaleTargetError(
                f"gapfill reprint photo_id={photo_id!r} product_type={product_type!r}: "
                f"build_pod_reprint refused ({result.reason})"
            )

    def _link(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        draft_id: str,
        pod_adapter: PodAdapter,
        pod_cfg: pod_config.PodConfig,
    ) -> None:
        """Step 3: ONLY this draft (a filter, never the fan-out)."""
        link_pod_drafts(
            conn,
            user_id,
            adapter=pod_adapter,
            print_file_host=self._host,
            cfg=pod_cfg,
            draft_ids={draft_id},
            attempt_tag=ATTEMPT_TAG,
        )

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        """Deactivate the published listing (fee not refunded). Never
        `delete_product`: that removes the connected Etsy listing and its
        history. A draft that was never published has nothing to undo."""
        after: dict = {}
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                after = e.payload.get("after") or {}
        if not after.get("published"):
            return
        if after.get("etsy_listing_id") is None:
            raise RuntimeError(f"action {action.action_id}: published listing has no etsy id")
        if self._adapter is None:
            raise RuntimeError("gapfill_reprint has no Etsy write adapter for undo")
        self._adapter.update_listing_state(int(after["etsy_listing_id"]), "inactive")

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        """The Etsy listing fee (injected from config); stored proposals carry
        0.0, so the governor takes max(action, this)."""
        return self._fee if self._fee is not None else action.estimated_cost_usd
