"""`listing.seo_edit` -- Claude rewrites a listing's title/tags/description,
the operator approves each edit (M8b slice 4a, design §4/§8 slice 4; widened
to cover description and expired-with-sales listings, see below). Policy:
Etsy E3 PERMITTED via `updateListing`/`listings_w`. Draft #10: title/tag edit
is a **T2 ceiling, NEVER higher** (the formula would say T1, but overridden --
§0 P3 policy surface + a bad edit resets a listing's Etsy search-ranking
history, which is not cleanly reversible even though the fields themselves
are; PRD §5.5 "wait at Gate 3"). `max_tier = Tier.PROPOSE` and this
capability is never promoted regardless of the ladder (registry.py's
invariant 2 -- max_tier is a Python attribute, not config).

**`description` editing is now implemented** (`EtsyListing.description`,
adapters/etsy/models.py, added purely additively/optionally -- every
already-stored `etsy.listing.observed` payload predates the field and still
validates fine, yielding `description=None`). The undo-honesty rule: a
proposed description is only ever kept when `target.current_description` is
not None AND the value actually differs -- a listing with no recorded
baseline (synced before this field existed) has any proposed description
silently dropped, so `undo_available=True` is never a lie. Title/tags may
still proceed on that same intent independently.

**Eligibility now also covers expired listings with genuine historical
sales** -- the same 7 real listings `listing.renew` targets for reactivation,
using the SAME config knob (`cfg.renew.min_lifetime_sales`) so the two
capabilities never disagree about "worth reviving". `views`/`num_favorers`
are NEVER used to gate this branch: confirmed empirically that Etsy's API
returns `views: 0` for every expired listing regardless of real sales
history.

**A third branch flags active listings with (near-)zero tags, regardless of
views or sale-window** (operator report, 2026-08: two real live listings had
0-1 lifetime views AND `current_tags == []`, falling through both branches
above). A listing with no tags is search-invisible by construction -- Etsy
cannot surface it for ANY query -- so "not enough traffic to judge yet"
never applies; the defect is provable without waiting on views. Gated by
`cfg.seo_edit.min_tags_before_flagged` (default 0 -- the operator's exact
ask was "zero tags", not "few tags"). Checked FIRST in `_eligible()` (the
more urgent defect) and shares the same `if key in out: continue` de-dup
guard the other branches use, so a listing already claimed by branch 1
(active + enough views) is never flagged twice.

**A fourth branch flags active, TAGGED listings whose tags are MISALIGNED
with what Etsy's ranker actually rewards** (operator report, 2026-08-25 --
`keyword_probe.py`'s free first-party signal makes this newly measurable).
"Has 13 tags" (passes branch 3) and "0 views" (fails branch 2's floor) used
to leave a real listing invisible to every existing branch even though its
tags are provably the wrong ones (real example: 13 tags, only 3 overlap the
ranker-rewarded set the probe found for a matching phrase, vs. a sibling
listing at 9/13 overlap that is genuinely well-optimized and must NOT be
flagged). Gated on `keyword_probe.listing_keyword_signal()` returning a
FRESH, title-matching signal (absence -- never probed, or the freshest
matching reading has aged out -- means NOT a candidate, same
absence-is-not-zero rule that function already applies) AND the listing's
current tags overlapping that signal's `ranker_tags` at or below
`cfg.seo_edit.min_ranker_tag_overlap` tags (see that field's own docstring
in models.py for the threshold and why it separates the two real examples
above). Checked AFTER the zero-tags branch (so a genuinely untagged listing
still gets that more urgent, clearer message) and AFTER the views/refresh
branch (so a listing the views branch already claims keeps that reason,
never silently swapped for the misalignment one) -- same `if key in out:
continue` de-dup guard. `views`/sale-window are NEVER checked here: a
never-viewed but genuinely misaligned listing (the real motivating case)
must not wait on traffic that its bad tags are precisely what's suppressing.

Execute()-time re-validation deliberately does NOT require the probe to
still be fresh (`execute()`'s own docstring/comment below): probe
freshness is an artifact of when this shop last happened to run keyword
research, not a fact about the target that can genuinely go stale between
an operator approving a proposal and it executing days later -- unlike
every other branch's gating condition (views, sale-window, tag count,
listing state), which really can change and legitimately terminalize a
stale action. Baking a time-decaying research cache's age into the ONE
grounding function `execute()` re-validates against is exactly the
policy-gate-inside-a-re-validated-grounding-function shape this chassis's
own guardrail history says never to repeat -- this module's own T6
cooldown_days precedent (docstring above) already handles an analogous
timing concern the same way: outside `_eligible()`, never re-checked here.
Concretely: `execute()` re-tries `_eligible()` with the probe's staleness
window widened before ever raising `StaleTargetError`, so a probe that
merely aged out between propose and approve never terminalizes an
otherwise-still-valid action.

**Planner-only, like `ops.tune_threshold`'s trigger is SQL but this
capability's COPY is not**: `propose()` always returns `[]` -- writing good
SEO copy is exactly the "deterministic heuristic too blunt" case (design
§11.1), there is no sensible deterministic title/tags/description to
generate. All the value is in `materialize()`: the LLM writes the copy,
`_validate_params()` validates it against Etsy's real field limits (dropped,
never clamped) and against the listing's current title/tags/description (a
no-op proposal is never built).

**Both digital AND POD listings are eligible** -- unlike `listing.reprice`
(digital-only, draft #9b), SEO edit only ever calls `update_listing` with
title/tags, which does not touch SKUs, variation structure, price, state,
or the production-partner declaration, so the POD-first rule ("never modify
provider-set SKU values or variation structure") is preserved regardless of
product type. `_eligible()` is the ONE grounding function shared by
propose() (which yields nothing) and materialize()/execute() -- the same
M8b slice-2 planner-safety contract every capability here follows.

Holds its own EtsyWriteAdapter, injected at construction (the chassis
contract -- autorenew.py precedent). This module never imports or
constructs an adapter itself."""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pydantic

from shopsteward.adapters.copy.tags import MAX_TAG_LEN as _MAX_TAG_LEN
from shopsteward.adapters.copy.tags import MAX_TAGS as _MAX_TAGS
from shopsteward.adapters.copy.tags import validate_tag
from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
from shopsteward.adapters.etsy.models import EtsyListing, EtsyListingUpdate
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.events import read_all
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.ops.config import get_ops_config, ops_config_hash
from shopsteward.pipeline.ops.keyword_probe import (
    ListingKeywordSignal,
    _is_safe_ranker_tag,
    listing_keyword_signal,
)
from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier
from shopsteward.pipeline.ops.registry import StaleTargetError, compute_action_id

# Etsy's real field limits. _MAX_TITLE_LEN has no tag-content counterpart
# (adapters/copy/tags.py owns MAX_TAGS/MAX_TAG_LEN, shared with
# adapters/copy/interface.py's CopyVerdict and pipeline/listings/models.py's
# GateEditFields).
_MAX_TITLE_LEN = 140
# A deliberate, conservative PRODUCT limit -- well below Etsy's real ceiling,
# not a value copied from Etsy's OpenAPI spec that could be wrong.
_MAX_DESCRIPTION_LEN = 5000


@dataclass(frozen=True)
class _Target:
    listing_id: int
    current_title: str
    current_tags: list[str]
    current_description: str | None
    lifetime_views: int
    reason: str
    # Only set (non-None) by branch 3 (tag misalignment) -- the ranker-tag
    # overlap count that made this listing a candidate. Every other branch
    # leaves this None; `misaligned_candidates()` uses its presence, never a
    # string-parse of `reason`, to identify branch-3 targets.
    ranker_tag_overlap: int | None = None


def _latest_observed(conn: sqlite3.Connection, user_id: int, listing_id: int) -> EtsyListing | None:
    """The most recent VALID etsy.listing.observed snapshot for this listing
    (autorenew.py precedent) -- the only place `tags` (not carried by
    proj_listings) is readable from."""
    latest: EtsyListing | None = None
    for e in read_live_observed(conn, "etsy.listing.observed"):
        if e.user_id != user_id or e.payload.get("listing_id") != listing_id:
            continue
        try:
            latest = EtsyListing.model_validate(e.payload)
        except pydantic.ValidationError:
            continue
    return latest


def _tag_overlap(current_tags: list[str], ranker_tags: list[str]) -> int:
    """Case-insensitive overlap count between a listing's current tags and
    a keyword-probe signal's `ranker_tags` -- Etsy tags are free-text and a
    casing difference (e.g. an operator-typed "Elk Wall Art" vs. a
    probe-observed "elk wall art") must never make a genuinely matching tag
    look misaligned."""
    return len({t.lower() for t in current_tags} & {t.lower() for t in ranker_tags})


def _misalignment_signal(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    listing_id: int,
    title: str,
    *,
    as_of: datetime | None,
    ignore_probe_staleness: bool = False,
) -> ListingKeywordSignal | None:
    """`keyword_probe.listing_keyword_signal()`, optionally with
    `keyword_probe.max_age_days` widened to effectively unbounded --
    `execute()`'s own re-validation uses `ignore_probe_staleness=True` so a
    probe merely aging out between propose and approve never terminalizes
    an otherwise-still-valid action (module docstring, execute()'s own
    comment below). `materialize()`/target-discovery callers always use the
    default (strict freshness) -- a BRAND NEW proposal must be grounded on
    current evidence."""
    probe_cfg = cfg
    if ignore_probe_staleness:
        # 999_999_999 -- timedelta's own max `days` magnitude (Python's
        # datetime.timedelta hard ceiling), i.e. as close to "unbounded" as
        # `keyword_probe.max_age_days`'s int type can express.
        probe_cfg = cfg.model_copy(
            update={
                "keyword_probe": cfg.keyword_probe.model_copy(update={"max_age_days": 999_999_999})
            }
        )
    return listing_keyword_signal(conn, user_id, probe_cfg, listing_id, title, as_of=as_of)


def _eligible(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    *,
    as_of: datetime | None = None,
    _ignore_probe_staleness: bool = False,
) -> dict[str, _Target]:
    """target_id -> the eligible-for-SEO-edit facts for that listing -- the
    ONE grounding function shared by materialize() and execute() so the two
    can never disagree. Four branches, checked in this order, never
    double-counting a listing (the `if key in out: continue` de-dup guard):

    1. Active with (near-)zero tags -- search-invisible by construction,
       checked FIRST since it's the more urgent defect and must never be
       skipped just because a later branch also would have claimed the
       listing (module docstring). `views`/sale-window are NEVER checked
       here.
    2. Active, viewed but not selling -> copy/SEO may be the problem (the
       same signal analytics.viewed_not_sold surfaces on the Brief, applied
       here with a revenue-window bound rather than lifetime, to match
       reprice's own eligibility shape).
    3. Active, tagged, but MISALIGNED with what Etsy's ranker rewards --
       checked AFTER 1/2 so a listing either already claims doesn't get a
       second, less-urgent reason (module docstring's fourth-branch
       section). `views`/sale-window are NEVER checked here either: this
       branch's whole point is to catch a listing branch 2's view floor
       can't reach.
    4. Expired with genuine historical sales -- the same bar `listing.renew`
       uses (`cfg.renew.min_lifetime_sales` against `proj_sale_items`, which
       only ever holds real, non-fixture sale line-items -- see renew.py's
       module docstring). `views` is NEVER checked for this branch (module
       docstring -- Etsy returns 0 for every expired listing regardless of
       real sales history).

    `_ignore_probe_staleness` (execute()-only, see `_misalignment_signal`'s
    own docstring): widens branch 3's probe-freshness requirement so a
    probe aging out between propose and approve never drops an
    already-approved target out of this dict. Never passed by
    materialize()/propose()/planner target-discovery -- those must always
    ground a NEW proposal on current evidence.

    No digital/POD split -- see module docstring."""
    start = datetime.now(UTC).date() - timedelta(days=cfg.windows.revenue_window_days - 1)
    end = datetime.now(UTC).date()

    out: dict[str, _Target] = {}

    active_rows = conn.execute(
        "SELECT listing_id, title, views FROM proj_listings WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall()
    for r in active_rows:
        key = str(r["listing_id"])
        listing = _latest_observed(conn, user_id, r["listing_id"])
        if listing is not None and len(listing.tags) <= cfg.seo_edit.min_tags_before_flagged:
            out[key] = _Target(
                listing_id=r["listing_id"],
                current_title=r["title"],
                current_tags=listing.tags,
                current_description=listing.description,
                lifetime_views=r["views"],
                reason=(
                    f"{r['title']} -- zero tags, invisible to Etsy search -- "
                    "add title/tags/description."
                ),
            )

    for r in active_rows:
        key = str(r["listing_id"])
        if key in out:
            continue  # already claimed by the zero-tags branch above
        if r["views"] < cfg.seo_edit.min_lifetime_views:
            continue
        sold_in_window = conn.execute(
            "SELECT 1 FROM proj_sale_items WHERE user_id=? AND listing_id=? "
            "AND sale_date BETWEEN ? AND ? LIMIT 1",
            (user_id, r["listing_id"], start.isoformat(), end.isoformat()),
        ).fetchone()
        if sold_in_window is not None:
            continue
        listing = _latest_observed(conn, user_id, r["listing_id"])
        if listing is None:
            continue  # defensive -- proj_listings is built from the same events
        out[str(r["listing_id"])] = _Target(
            listing_id=r["listing_id"],
            current_title=r["title"],
            current_tags=listing.tags,
            current_description=listing.description,
            lifetime_views=r["views"],
            reason=(
                f"{r['views']} views, 0 sales in the last "
                f"{cfg.windows.revenue_window_days}d -- refresh title/tags."
            ),
        )

    for r in active_rows:
        key = str(r["listing_id"])
        if key in out:
            continue  # already claimed by the zero-tags or refresh branch above
        listing = _latest_observed(conn, user_id, r["listing_id"])
        if listing is None or len(listing.tags) <= cfg.seo_edit.min_tags_before_flagged:
            continue  # untagged handled by branch 1; nothing to overlap-check either way
        signal = _misalignment_signal(
            conn,
            user_id,
            cfg,
            r["listing_id"],
            r["title"],
            as_of=as_of,
            ignore_probe_staleness=_ignore_probe_staleness,
        )
        if signal is None:
            continue  # no fresh, title-matching probe -- absence, not evidence of misalignment
        overlap = _tag_overlap(listing.tags, signal.ranker_tags)
        if overlap > cfg.seo_edit.min_ranker_tag_overlap:
            continue  # well-aligned -- not a candidate
        out[key] = _Target(
            listing_id=r["listing_id"],
            current_title=r["title"],
            current_tags=listing.tags,
            current_description=listing.description,
            lifetime_views=r["views"],
            reason=(
                f"{overlap} of {len(listing.tags)} tags match what Etsy's ranker rewards for "
                f"{signal.matched_phrases[0]!r} -- realign title/tags."
            ),
            ranker_tag_overlap=overlap,
        )

    expired_rows = conn.execute(
        "SELECT listing_id, title FROM proj_listings WHERE user_id=? AND state='expired'",
        (user_id,),
    ).fetchall()
    for r in expired_rows:
        key = str(r["listing_id"])
        if key in out:
            continue  # defensive -- a listing can't be both active and expired
        lifetime_sales = conn.execute(
            "SELECT COUNT(*) AS n FROM proj_sale_items WHERE user_id=? AND listing_id=?",
            (user_id, r["listing_id"]),
        ).fetchone()["n"]
        if lifetime_sales < cfg.renew.min_lifetime_sales:
            continue
        listing = _latest_observed(conn, user_id, r["listing_id"])
        if listing is None or listing.quantity < 1:
            # matches listing.renew's gate -- a sold-out listing isn't
            # "worth reviving" via renew, so seo_edit shouldn't polish it either.
            continue
        out[key] = _Target(
            listing_id=r["listing_id"],
            current_title=r["title"],
            current_tags=listing.tags,
            current_description=listing.description,
            lifetime_views=0,  # never a gating signal for this branch -- see module docstring
            reason=(
                f"{r['title']} -- expired, {lifetime_sales} lifetime sale(s) -- "
                "refresh title/tags/description."
            ),
        )
    return out


def misaligned_candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, *, as_of: datetime | None = None
) -> list[dict]:
    """listing_id/title/tag_count/overlap for every listing branch 3
    (tag-misalignment) of `_eligible()` currently claims -- exposed so
    planner.py's facts JSON (target discovery) and analytics.py's
    `data_quality_notes()` (operator visibility) share ONE computation
    rather than re-deriving `_eligible()`'s misalignment logic twice.
    Target discovery only, same "courtesy to the model/operator, not the
    safety boundary" every other planner.py discovery helper documents --
    materialize()'s own `_eligible()` still re-grounds."""
    return [
        {
            "listing_id": t.listing_id,
            "title": t.current_title,
            "tag_count": len(t.current_tags),
            "overlap": t.ranker_tag_overlap,
            "reason": t.reason,
        }
        for t in _eligible(conn, user_id, cfg, as_of=as_of).values()
        if t.ranker_tag_overlap is not None
    ]


def _validate_params(
    params: dict, target: _Target, cfg: OpsConfig
) -> dict[str, str | list[str]] | None:
    """Structural validation against Etsy's real limits (drop, never clamp)
    plus a diff against `target`'s current title/tags/description -- returns
    only the fields that are both valid AND actually changed, or None if
    nothing survives. Shared by materialize() and execute() (re-validation)
    so the two rules can never drift apart.

    description's undo-honesty rule (module docstring): kept ONLY IF
    `target.current_description is not None` AND the proposed value differs
    from it. No baseline (a listing synced before this field existed, or
    Etsy genuinely has none) -> the description key is dropped, never kept
    with no way to restore it on undo -- title/tags may still proceed on the
    same intent independently. Description content does not need tags'
    comma-rejection check (that's specific to comma-joining tags into Etsy's
    form-urlencoded array field; description is a plain string field, sent
    as-is).

    M2 (guardrail review 2026-08-25): `tags` also runs through
    `keyword_probe._is_safe_ranker_tag` -- the SAME misrepresentation filter
    the facts-JSON side already applies to probe-derived candidates
    (planner.py's `_seo_edit_keyword_signals`), reused here so the guard is
    deterministic on BOTH ends rather than trusting the prompt: this change
    puts competitor medium-vocabulary (from keyword-probe facts) directly
    next to the tag-writing task, and nothing stops the LLM composing e.g.
    "bison painting" itself rather than copying it from a fact block.

    Chose REJECT-THE-WHOLE-TAGS-UPDATE over silently dropping just the
    offending tag: this function's existing structural rule for `tags`
    already rejects the entire update on ANY single invalid tag (comma/
    length) rather than repairing the list item-by-item -- reusing that same
    shape means one validation semantics for the whole field, not two. More
    importantly, `tags` is exactly what the operator reviews and approves
    (T2/PROPOSE ceiling, module docstring); silently publishing a
    LLM-authored list with one entry quietly removed is a different tag set
    than what was proposed and approved, without the operator ever being
    told. Title/description are untouched and may still proceed on the same
    intent independently, same as every other per-field independence rule
    here."""
    title = params.get("title")
    new_title: str | None = None
    if title is not None:
        if not isinstance(title, str) or not (1 <= len(title) <= _MAX_TITLE_LEN):
            return None
        new_title = title

    tags = params.get("tags")
    new_tags: list[str] | None = None
    if tags is not None:
        if not isinstance(tags, list) or not (1 <= len(tags) <= _MAX_TAGS):
            return None
        if not all(isinstance(t, str) for t in tags):
            return None
        try:
            for t in tags:
                validate_tag(t, max_len=_MAX_TAG_LEN)
        except ValueError:
            return None  # drop, never clamp -- see module docstring
        if not all(_is_safe_ranker_tag(t, cfg) for t in tags):
            return None  # M2: misrepresentation -- reject the whole update, see docstring above
        new_tags = tags

    description = params.get("description")
    new_description: str | None = None
    if (
        description is not None
        and isinstance(description, str)
        and 1 <= len(description) <= _MAX_DESCRIPTION_LEN
    ):
        new_description = description
    # else: structurally invalid -- silently dropped, never clamped.

    changed: dict[str, str | list[str]] = {}
    if new_title is not None and new_title != target.current_title:
        changed["title"] = new_title
    if new_tags is not None and new_tags != target.current_tags:
        changed["tags"] = new_tags
    if (
        new_description is not None
        and target.current_description is not None
        and new_description != target.current_description
    ):
        changed["description"] = new_description
    return changed or None


def _build_action(
    target: _Target,
    changed: dict[str, str | list[str]],
    cfg_hash: str,
    today: str,
    expires_at: str,
) -> ProposedAction:
    raw = "|".join((str(target.listing_id), json.dumps(changed, sort_keys=True)))
    inputs_hash = hashlib.sha256(raw.encode()).hexdigest()
    action_id = compute_action_id(
        "listing.seo_edit", str(target.listing_id), inputs_hash, cfg_hash, today
    )
    return ProposedAction(
        action_id=action_id,
        capability="listing.seo_edit",
        target_type="listing",
        target_id=str(target.listing_id),
        tier=Tier.PROPOSE,  # overwritten by the runner with the effective tier
        reason=target.reason,
        inputs_hash=inputs_hash,
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=expires_at,
        params=changed,
    )


class ListingSeoEdit:
    key = "listing.seo_edit"
    # T2 ceiling -- NEVER promotable (draft #10: a bad SEO edit resets Etsy's
    # search-ranking history, which isn't cleanly reversible even though the
    # fields are). registry.py's invariant 2 enforces there is no config path
    # that can raise this.
    max_tier = Tier.PROPOSE
    policy_verified = True  # Etsy E3 permitted.

    def __init__(self, adapter: EtsyWriteAdapter) -> None:
        self._adapter = adapter

    def propose(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig
    ) -> list[ProposedAction]:
        return []  # planner-only -- see module docstring.

    def materialize(
        self, conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        target = _eligible(conn, user_id, cfg).get(intent.target_id)
        if target is None:
            return None  # ungrounded (hallucinated or ineligible target)

        changed = _validate_params(intent.params, target, cfg)
        if changed is None:
            return None  # invalid, or no actual change -- dropped, never clamped

        today_date = datetime.now(UTC).date()
        today = today_date.isoformat()
        cfg_hash = ops_config_hash(cfg)
        expires_at = (today_date + timedelta(days=cfg.autonomy.proposal_ttl_days)).isoformat()
        return _build_action(target, changed, cfg_hash, today, expires_at)

    def execute(
        self, conn: sqlite3.Connection, user_id: int, action: ProposedAction
    ) -> ExecutionResult:
        listing_id = int(action.target_id)
        cfg = get_ops_config(conn, user_id)
        target = _eligible(conn, user_id, cfg).get(action.target_id)
        if target is None:
            # Branch-3 (misalignment) fallback: retry with the probe's
            # staleness window widened before ever declaring this target
            # genuinely gone -- a probe merely aging out between propose and
            # approve is a research-cache artifact, not a real change to the
            # target, and must never terminalize an otherwise-still-valid
            # action (module docstring's execute()-time re-validation
            # section, `_misalignment_signal`'s own docstring). If the
            # listing is ALSO no longer active/tagged/misaligned for a real
            # reason, this retry finds nothing either and the branch below
            # still raises.
            target = _eligible(conn, user_id, cfg, _ignore_probe_staleness=True).get(
                action.target_id
            )
        if target is None:
            # StaleTargetError (H2b, guardrail review 2026-08-25): genuine
            # per-target staleness -> the runner terminalizes this one.
            raise StaleTargetError(
                f"listing {listing_id}: no longer active/eligible -- refusing edit"
            )

        changed = _validate_params(action.params, target, cfg)
        if changed is None:
            # Deliberately plain ValueError, not StaleTargetError -- a bad
            # PARAMS problem (same class as reprice.py's/caption_draft.py's
            # own invalid-params checks), not the target being stale, so the
            # runner's safe default (non-terminal refusal) applies.
            raise ValueError(
                f"action {action.action_id}: params {action.params!r} are no longer valid or "
                "a no-op -- refusing edit"
            )

        before: dict[str, str | list[str]] = {}
        if "title" in changed:
            before["title"] = target.current_title
        if "tags" in changed:
            before["tags"] = target.current_tags
        if "description" in changed:
            # guaranteed non-None -- _validate_params only ever keeps
            # "description" in `changed` when current_description is not None.
            before["description"] = target.current_description

        self._adapter.update_listing(
            listing_id,
            EtsyListingUpdate(
                title=changed.get("title"),
                tags=changed.get("tags"),
                description=changed.get("description"),
            ),
        )
        return ExecutionResult(before=before, after=dict(changed), cost_usd=0.0, duration_ms=0)

    def undo(self, conn: sqlite3.Connection, user_id: int, action: ProposedAction) -> None:
        prior: dict | None = None
        for e in read_all(conn, "action.executed"):
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id:
                prior = e.payload["before"]
        if prior is None:
            return  # nothing was actually executed -- runner already guards this, but be safe

        listing_id = int(action.target_id)
        self._adapter.update_listing(
            listing_id,
            EtsyListingUpdate(
                title=prior.get("title"),
                tags=prior.get("tags"),
                description=prior.get("description"),
            ),
        )

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return 0.0
