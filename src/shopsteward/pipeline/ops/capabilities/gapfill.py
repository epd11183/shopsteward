"""`listing.gapfill_reprint` -- gap-fill step 2 (M8b, design §4/§8): Claude
proposes reprinting a PROVEN best-seller into a NEW POD format; on approval,
`execute()` calls `build_pod_reprint` (pod/build.py, gap-fill step 1, merged)
OFFLINE -- the reprint POD draft is created locally, stopping at
`print_file_hosted`, exactly like every other POD build. **Zero live surface
here**: the reprint reaches Gate 3 (and any live Gelato/Etsy call) only later,
via the operator's existing, already-gated `shop build` link step -- this
capability never runs link/enrich/push.

T2 ceiling, never auto/promotable (`max_tier = Tier.PROPOSE`, registry.py's
invariant 2 -- creating catalog stays operator-approved, full stop) and
**`undo = None`** explicitly: allowed because max_tier is not below PROPOSE
(registry.py invariant 1), and there is nothing to reverse programmatically
anyway -- draft #16: "a capability whose output is an unpublished Etsy DRAFT
needs no autonomy tier -- Gate 3 is already its approval." The operator's
reversal is simply declining to publish at Gate 3.

`_candidates()` is the ONE grounding function shared by propose()/
materialize() (M8b slice-2 planner-safety contract, every capability here
follows it): the reprint ceiling is honest -- only a listing that is (1) a
PROVEN listing (`analytics.proven_listings` -- T12 2026-08-25: a sale within
the trailing 90 days, OR at least one lifetime sale, OR a views-velocity
signal on a zero-sales listing; see that function's docstring), AND
(2) has an archived, reprintable source (`source_assets.resolve_source` ->
`archived=True`), is ever proposed for ANY missing POD product_type. A
best-seller with no archived source is simply not reprintable; no proposal,
no guess. Draft existence is checked by computing the SAME `draft_id`
`build_pod_reprint` would (photo_id|pod_config_hash|provider|product_type) --
a product_type the photo already has a draft for is never re-proposed.

Holds a `print_file_host` injected at construction (the chassis contract --
autorenew.py precedent), but unlike the Etsy-write capabilities this is
ALWAYS the offline host in practice (the CLI passes
`build_print_file_host(live=False)` unconditionally for this capability --
see cli.py): `build_pod_reprint` only needs a host to reach
`print_file_hosted` locally; nothing here ever reaches Gelato or Etsy. This
module constructs no adapter/host itself."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.adapters.printfile.interface import PrintFileHost
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod.build import build_pod_reprint
from shopsteward.pipeline.listings.source_assets import resolve_source
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import compute_action_id


def _draft_exists(conn: sqlite3.Connection, user_id: int, draft_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND draft_id=? LIMIT 1",
            (user_id, draft_id),
        ).fetchone()
        is not None
    )


def _candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
) -> dict[str, ProposedAction]:
    """`f"{listing_id}:{product_type}"` -> the ProposedAction propose() would
    build for it -- see module docstring for the honest reprint ceiling
    (proven seller AND archived source AND missing format, no exceptions)."""
    sellers = analytics.proven_listings(conn, user_id, cfg)
    if not sellers:
        return {}

    pod_cfg = pod_config.get_pod_config(conn, user_id)
    pod_cfg_hash = pod_config.pod_config_hash(pod_cfg)
    # product_type -> its (single) provider in the catalog -- pod.json's
    # `catalog` is keyed by provider name (pod/models.py's PodProviderCatalog).
    # ponytail: assumes a 1:1 product_type -> provider catalog (true today,
    # every product_type in pod.json has exactly one provider). Under a
    # future many-providers catalog this dedup-only `provider` pick could
    # differ from the provider build_pod_reprint's own variant selection
    # actually uses -- at worst a noisy proposal that fails loudly at
    # execute() (build_pod_reprint's own guard), never a duplicate draft.
    # Upgrade if/when a product_type ever routes to >1 provider.
    provider_by_type: dict[str, str] = {}
    for provider, provider_cat in pod_cfg.catalog.items():
        for product_type in provider_cat.products:
            provider_by_type.setdefault(product_type, provider)

    today_date = datetime.now(UTC).date()
    today = today_date.isoformat()
    expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()

    out: dict[str, ProposedAction] = {}
    for seller in sellers:
        source = resolve_source(conn, user_id, seller.listing_id)
        if source is None or not source.archived or source.photo_id is None:
            # Not reprintable -- no archived source. Honest ceiling: never
            # guess a reprint for a best-seller we can't actually rebuild.
            continue

        for product_type, provider in provider_by_type.items():
            draft_id = hashlib.sha256(
                f"{source.photo_id}|{pod_cfg_hash}|{provider}|{product_type}".encode()
            ).hexdigest()
            if _draft_exists(conn, user_id, draft_id):
                continue  # this photo already has (or is building) this format

            target_id = f"{seller.listing_id}:{product_type}"
            raw = "|".join((source.photo_id, product_type, str(seller.units)))
            inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
            action_id = compute_action_id(
                "listing.gapfill_reprint", target_id, inputs_hash, pod_cfg_hash, today
            )
            # M2 (guardrail review, 2026-08-25): the trailing clause must be
            # as conditional as `analytics.proof_phrase()`'s own prefix --
            # this reason lands verbatim on the Gate-3 card that authorizes
            # a REAL PAID POD SKU. `seller.units == 0` is only possible via
            # `proven_listings()`'s views-velocity arm (proof_phrase's own
            # docstring): that listing has never sold, so it must never be
            # called "the proven winner".
            trailing = (
                "reprint the proven winner."
                if seller.units > 0
                else "print a first physical copy -- rising views, still no sale."
            )
            out[target_id] = ProposedAction(
                action_id=action_id,
                capability="listing.gapfill_reprint",
                target_type="listing_reprint",
                target_id=target_id,
                tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
                reason=f"{analytics.proof_phrase(seller)}; no {product_type} yet -- {trailing}",
                inputs_hash=inputs_hash,
                estimated_cost_usd=0.0,
                undo_available=False,
                expires_at=expires_at,
                params={
                    "photo_id": source.photo_id,
                    "product_type": product_type,
                    "listing_id": seller.listing_id,
                },
            )
    return out


class ListingGapfillReprint:
    key = "listing.gapfill_reprint"
    # T2 ceiling -- NEVER promotable (creates catalog; draft #16 keeps this
    # operator-approved regardless of the ladder). registry.py's invariant 2
    # enforces there is no config path that can raise this Python ceiling.
    max_tier = Tier.PROPOSE
    policy_verified = True  # Etsy E5 -- an unpublished draft; Gate 3 is the publish authority.
    # No undo path: allowed because max_tier is not below PROPOSE
    # (registry.py invariant 1) -- the reversal is Gate 3 (decline to
    # publish), never a programmatic undo (draft #16).
    undo = None

    def __init__(self, print_file_host: PrintFileHost) -> None:
        self._host = print_file_host

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return list(_candidates(conn, user_id, cfg).values())

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        return _candidates(conn, user_id, cfg).get(intent.target_id)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        photo_id = str(action.params["photo_id"])
        product_type = str(action.params["product_type"])
        pod_cfg = pod_config.get_pod_config(conn, user_id)

        result = build_pod_reprint(
            conn,
            user_id,
            photo_id,
            product_type,
            print_file_host=self._host,
            pod_cfg=pod_cfg,
        )
        if not result.built:
            # Became ineligible or already exists between propose() and
            # approval -- must fail loudly (action.failed), never silently
            # claim a draft that was never actually built.
            raise ValueError(
                f"gapfill reprint photo_id={photo_id!r} product_type={product_type!r}: "
                f"build_pod_reprint refused ({result.reason})"
            )

        return ExecutionResult(
            before={"draft_exists": False},
            after={"draft_id": result.draft_id, "product_type": product_type},
            cost_usd=0.0,
            duration_ms=0,
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
