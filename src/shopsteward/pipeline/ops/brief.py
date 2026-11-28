"""Assemble + render the read-only shop brief (design §6's "THE SHOP"
section, plus the NEEDS YOU / DONE / REFUSED / AUTONOMY chassis sections
added in PR3, M8a spec §8 PR3 / draft §6). The chassis sections read
proj_actions/proj_capability_state + governor.month_spend()/is_halted() --
still PURE, no LLM, no network, exactly like THE SHOP's analytics.py reads.

generate_brief() is deterministic (PURE, no LLM, no network -- design §7).
render_text() is a template, not a model."""

import contextlib
import sqlite3
from datetime import UTC, date, datetime

from shopsteward.core.events import read_all
from shopsteward.pipeline import llm_ledger
from shopsteward.pipeline.ops import analytics, buffer_publish, cadence, governor, outcomes
from shopsteward.pipeline.ops.capabilities import autorenew_on, reprice
from shopsteward.pipeline.ops.models import (
    ActionOutcome,
    Brief,
    BriefAction,
    BriefAutonomy,
    BriefCaption,
    BriefLadderRow,
    BriefPin,
    BriefProposal,
    BriefQueued,
    BriefQueuedPost,
    BriefReadbackFailure,
    BriefRefusal,
    BriefSocialQueue,
    BriefUnresolvedIntent,
    OpsConfig,
    ProposedAction,
    RefusalReason,
    Scorecard,
    Tier,
)
from shopsteward.pipeline.ops.projections import (
    _action_id_from_destination_url,
    action_rows,
    capability_states,
)
from shopsteward.pipeline.ops.registry import REGISTRY, Capability
from shopsteward.pipeline.ops.scorecard import build_scorecard
from shopsteward.pipeline.ops.tiers import effective_tier, effective_tier_for

# ponytail: fixed 7-day lookback for DONE/REFUSED, not a config knob -- add
# one under cfg.windows if the operator ever wants a longer/shorter Brief
# history.
_DONE_REFUSED_WINDOW_DAYS = 7
_BUFFER_METRIC_KEYS = ("impressions", "saves", "reactions")


def _proposed_by_action_id(conn: sqlite3.Connection, user_id: int) -> dict[str, ProposedAction]:
    """action.proposed carries fields (expires_at, undo_available) that
    proj_actions does not persist -- read straight from the event log, same
    precedent as governor.py's own reads."""
    out: dict[str, ProposedAction] = {}
    for e in read_all(conn, "action.proposed"):
        if e.user_id == user_id:
            out[e.payload["action_id"]] = ProposedAction.model_validate(e.payload)
    return out


def _within_window(created_at: str | None, as_of: date, days: int) -> bool:
    if not created_at:
        return False
    day = date.fromisoformat(created_at[:10])
    return 0 <= (as_of - day).days <= days


_AUTO_TIERS = (Tier.AUTO, Tier.NOTIFY)  # runner._AUTO_EXECUTE_TIERS


class _AssumeReady:
    """Governor stand-in: the brief runs with the offline REGISTRY (no live
    adapters), so the real capability's `precondition_ok` is always False. The
    weekly run is live, so the question here is only "do the GUARDS clear"."""

    def __init__(self, cap: Capability) -> None:
        self._cap = cap
        self.key = cap.key
        self.max_tier = cap.max_tier
        self.policy_verified = cap.policy_verified
        self.precondition_ok = True

    def estimate_cost_usd(self, action: ProposedAction) -> float:
        return self._cap.estimate_cost_usd(action)


def _refusal_text(reason: RefusalReason, cap_key: str, cfg: OpsConfig) -> str:
    if reason == RefusalReason.PACE and cap_key == "listing.gapfill_reprint":
        return (
            f"gapfill weekly pace: {cfg.gapfill.max_per_week} new Gelato product(s) per ISO "
            "week already used, or another gapfill draft is in flight"
        )
    if reason == RefusalReason.PACE:
        return "weekly/channel pace already used"
    return f"governor refuses: {reason.value}"


def _queue_verdict(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    cap: Capability,
    action: ProposedAction,
    as_of: date,
) -> tuple[bool, str]:
    """(will the next run execute it, why). Calls the guards -- never copies
    them: the reprice blackout/cooldown and autorenew_on's sold-in-window check
    live in their capability modules, everything else in `governor.govern`
    (record=False: the brief must not append `action.refused`)."""
    if not cfg.autonomy.enabled:
        return False, "autonomy is OFF -- the run does nothing"
    if cap.key == "listing.reprice":
        if reprice._in_blackout(cfg, as_of):
            return False, f"reprice blackout until {cfg.reprice.blackout_until}"
        if int(action.target_id) in reprice._cooling_down(conn, user_id, cfg, as_of):
            return False, (
                f"reprice revert cooldown ({cfg.reprice.revert_cooldown_days}d after an undo "
                "or a restored price)"
            )
    elif cap.key == "listing.autorenew_on":
        if int(action.target_id) not in autorenew_on.sold_in_window_ids(conn, user_id, cfg, as_of):
            return False, (
                f"autorenew_on guard: no sale in the last {cfg.dead_listing.window_days}d "
                "(recurring renewal fees need a recent sale)"
            )
        if action.target_id not in autorenew_on._candidates(conn, user_id, cfg):
            return False, "no longer eligible (renewal posture changed or auto-renew already on)"
    elif cap.key == "listing.gapfill_reprint":
        from shopsteward.pipeline.ops.capabilities import gapfill

        if action.target_id not in gapfill._candidates(conn, user_id, cfg):
            return False, (
                "no longer eligible (format not buildable for this photo, already built, "
                "or attempts exhausted) -- the run will close it as stale"
            )
    decision = governor.govern(conn, user_id, action, _AssumeReady(cap), cfg, as_of, record=False)
    if not decision.approved and decision.reason is not None:
        return False, _refusal_text(decision.reason, cap.key, cfg)
    return True, "guards clear -- executes in the next run (live gates permitting)"


def _split_pending(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> tuple[list[BriefProposal], list[BriefQueued]]:
    """Open proposals -> (NEEDS YOU, QUEUED). Mirrors the runner's own tier
    decision: effective tier from the registry + `autonomy.tier_floor`, then
    the per-action `auto_hold` clamp back to PROPOSE. A capability that is not
    registered cannot be judged, so it stays with the operator."""
    proposed = _proposed_by_action_id(conn, user_id)
    states = capability_states(conn, user_id)
    needs: list[BriefProposal] = []
    queued: list[BriefQueued] = []
    for row in action_rows(conn):
        if row["user_id"] != user_id or row["state"] != "proposed":
            continue
        p = proposed.get(row["action_id"])
        if p is None or as_of > date.fromisoformat(p.expires_at):
            continue  # never proposed (shouldn't happen) or expired
        item = BriefProposal(
            action_id=row["action_id"],
            capability=row["capability"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            tier=Tier(row["tier"]),
            reason=row["reason"],
            expires_at=p.expires_at,
        )
        cap = REGISTRY.get(row["capability"])
        eff = (
            effective_tier(cap, states.get(cap.key), cfg.autonomy.tier_floor)
            if cap is not None
            else item.tier
        )
        if cap is None or eff not in _AUTO_TIERS or p.params.get("auto_hold"):
            needs.append(item)
            continue
        will, why = _queue_verdict(conn, user_id, cfg, cap, p, as_of)
        queued.append(BriefQueued(**item.model_dump(), will_execute=will, verdict=why))
    return needs, queued


def _readback_failed(conn: sqlite3.Connection, user_id: int) -> list[BriefReadbackFailure]:
    """Drafts whose LATEST read-back failed and that were never published
    since (event-derived, so the brief needs no projection rebuild)."""
    latest: dict[str, list[str] | None] = {}
    for e in read_all(conn, "listingdraft.readback"):
        if e.user_id == user_id:
            p = e.payload
            latest[p["draft_id"]] = None if p["ok"] else list(p["problems"])
    published = {
        e.payload["draft_id"] for e in read_all(conn, "gate3.published") if e.user_id == user_id
    }
    out = []
    for draft_id, problems in latest.items():
        if problems is None or draft_id in published:
            continue
        row = None
        # listings projection may not be built in this conn -- id/title are cosmetic
        with contextlib.suppress(sqlite3.OperationalError):
            row = conn.execute(
                "SELECT etsy_listing_id, title FROM proj_listing_drafts "
                "WHERE user_id=? AND draft_id=?",
                (user_id, draft_id),
            ).fetchone()
        out.append(
            BriefReadbackFailure(
                draft_id=draft_id,
                etsy_listing_id=row["etsy_listing_id"] if row else None,
                title=row["title"] if row else None,
                problems=problems,
            )
        )
    return out


def _done_recent(conn: sqlite3.Connection, user_id: int, as_of: date) -> list[BriefAction]:
    proposed = _proposed_by_action_id(conn, user_id)
    out = []
    for row in action_rows(conn):
        if row["user_id"] != user_id or row["state"] != "executed":
            continue
        if not _within_window(row["resolved_at"], as_of, _DONE_REFUSED_WINDOW_DAYS):
            continue
        p = proposed.get(row["action_id"])
        out.append(
            BriefAction(
                action_id=row["action_id"],
                capability=row["capability"],
                target_id=row["target_id"],
                reason=row["reason"],
                tier=Tier(row["tier"]),
                undo_available=p.undo_available if p is not None else False,
            )
        )
    return out


def _refused_recent(conn: sqlite3.Connection, user_id: int, as_of: date) -> list[BriefRefusal]:
    proposed = _proposed_by_action_id(conn, user_id)
    out = []
    for e in read_all(conn, "action.refused"):
        if e.user_id != user_id or not _within_window(
            e.created_at, as_of, _DONE_REFUSED_WINDOW_DAYS
        ):
            continue
        p = proposed.get(e.payload["action_id"])
        if p is None:
            continue  # a refusal for an action we never saw proposed -- ignore
        out.append(
            BriefRefusal(capability=p.capability, target_id=p.target_id, reason=e.payload["reason"])
        )
    return out


def _caption_drafts(conn: sqlite3.Connection, user_id: int, as_of: date) -> list[BriefCaption]:
    """Recent `social.caption_drafted` events (M8b slice 6) -- the operator's
    copy-paste queue. Same 7-day lookback as DONE/REFUSED (no config knob
    yet, same ponytail as _DONE_REFUSED_WINDOW_DAYS above). T5+E5
    (2026-08-25): excludes any draft whose OWN `action_id` has a
    `social.caption_posted` event -- `_pin_drafts()`'s own exclusion
    precedent below, made possible now that captions have a mark-posted
    signal too (`capabilities/caption_draft.py`'s `mark_posted()`)."""
    posted_action_ids = {
        e.payload.get("action_id")
        for e in read_all(conn, "social.caption_posted")
        if e.user_id == user_id
    }
    out = []
    for e in read_all(conn, "social.caption_drafted"):
        if e.user_id != user_id or not _within_window(
            e.created_at, as_of, _DONE_REFUSED_WINDOW_DAYS
        ):
            continue
        if e.payload.get("publish_via") == "buffer":
            continue  # queued in Buffer, not a hand-post item (double-post guard)
        action_id = e.payload.get("action_id")
        if action_id is not None and action_id in posted_action_ids:
            continue  # marked posted -- drop from the copy-paste queue
        out.append(
            BriefCaption(
                listing_id=e.payload["listing_id"],
                channel=e.payload.get("channel", "instagram"),
                title=e.payload["title"],
                caption=e.payload["caption"],
                drafted_at=e.payload["drafted_at"],
                action_id=action_id,
            )
        )
    return out


def _pin_action_ids_by_listing(conn: sqlite3.Connection, user_id: int) -> dict[int, list[str]]:
    """listing_id -> executed `social.pinterest_post` action_ids for this
    user -- the same action_rows()-derived lookup projections.py's
    `_pin_experiment_rows()` builds, mirrored here (not imported) since it's
    a query over action_rows(), not a helper function, per that module's own
    docstring precedent."""
    out: dict[int, list[str]] = {}
    for row in action_rows(conn):
        if (
            row["user_id"] != user_id
            or row["capability"] != "social.pinterest_post"
            or row["state"] != "executed"
        ):
            continue
        try:
            listing_id = int(row["target_id"])
        except ValueError:
            continue
        out.setdefault(listing_id, []).append(row["action_id"])
    return out


def _pin_drafts(conn: sqlite3.Connection, user_id: int, as_of: date) -> list[BriefPin]:
    """Recent `social.pin_drafted` events (Variant A, 2026-08-24 design doc
    §2) -- the operator's copy-paste-to-Pinterest queue. Same 7-day
    lookback as DONE/REFUSED/captions (no config knob yet, same ponytail as
    `_DONE_REFUSED_WINDOW_DAYS`). Excludes any draft whose OWN action_id (not
    just its listing_id -- a listing can have more than one pin over time,
    cooldown-permitting) has a `social.pin_posted` event, i.e. the operator
    has already run `ops mark-posted` for it."""
    posted_action_ids = {
        e.payload.get("action_id")
        for e in read_all(conn, "social.pin_posted")
        if e.user_id == user_id
    }
    action_ids_by_listing = _pin_action_ids_by_listing(conn, user_id)

    out = []
    for e in read_all(conn, "social.pin_drafted"):
        if e.user_id != user_id or not _within_window(
            e.created_at, as_of, _DONE_REFUSED_WINDOW_DAYS
        ):
            continue
        if e.payload.get("publish_via") == "buffer":
            continue  # queued in Buffer, not a hand-post item (double-post guard)
        listing_id = e.payload["listing_id"]
        prefix = _action_id_from_destination_url(e.payload.get("destination_url"))
        action_id = next(
            (
                aid
                for aid in action_ids_by_listing.get(listing_id, [])
                if prefix is not None and aid.startswith(prefix)
            ),
            None,
        )
        if action_id is not None and action_id in posted_action_ids:
            continue  # marked posted -- drop from the copy-paste queue
        out.append(
            BriefPin(
                listing_id=listing_id,
                title=e.payload["title"],
                description=e.payload["description"],
                alt_text=e.payload["alt_text"],
                board_key=e.payload["board_key"],
                destination_url=e.payload["destination_url"],
                image_url=e.payload["image_url"],
                drafted_at=e.payload["drafted_at"],
                action_id=action_id,
            )
        )
    return out


def _social_queue(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> BriefSocialQueue:
    """S8: our posts waiting in Buffer (due + undo-until), per-channel weekly
    deficits, unresolved publish intents and Buffer error-status posts. All
    event-derived (buffer-sync is what keeps the events fresh)."""
    observed = cadence.latest_observed(conn, user_id)
    deleted = {
        e.payload.get("buffer_post_id")
        for e in read_all(conn, buffer_publish.DELETED)
        if e.user_id == user_id
    }
    queued = []
    for e in read_all(conn, buffer_publish.QUEUED):
        p = e.payload
        obs = observed.get(str(p["buffer_post_id"]))
        if e.user_id != user_id or p["buffer_post_id"] in deleted:
            continue
        if obs is not None and obs.get("status") != "scheduled":
            continue  # sent (nothing to undo) or errored (listed below)
        queued.append(
            BriefQueuedPost(
                action_id=p["action_id"],
                channel=p["channel"],
                listing_id=p["listing_id"],
                due_at=p["due_at"],
                undo_until=p["due_at"],
                reconciled=bool(p.get("reconciled")),
            )
        )
    queued.sort(key=lambda q: q.due_at)
    unresolved = [
        BriefUnresolvedIntent(
            action_id=i.action_id,
            listing_id=i.event.payload["listing_id"],
            channel=i.event.payload["channel"],
            drafted_at=i.event.payload["drafted_at"],
        )
        for i in buffer_publish.unresolved_intents(conn, user_id)
    ]
    errors = [
        f"{o['channel']} post {o['post_id']} is in error status (not retried)"
        for o in observed.values()
        if o.get("status") == "error"
        # a post that errored while still due in the future is recent too
        and (
            not o.get("due_at")
            or (as_of - date.fromisoformat(o["due_at"][:10])).days <= _DONE_REFUSED_WINDOW_DAYS
        )
    ]
    return BriefSocialQueue(
        queued=queued,
        deficits=cadence.cadence_deficit(conn, user_id, cfg, as_of),
        unresolved=unresolved,
        errors=sorted(errors),
    )


def _autonomy_section(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> BriefAutonomy:
    ladder = [
        BriefLadderRow(
            capability=cap_key,
            tier=effective_tier_for(cap_key, st, cfg.autonomy.tier_floor),
            approvals=st.approvals,
            rejections=st.rejections,
            executions=st.executions,
            undos=st.undos,
            tier_since=st.tier_since,
        )
        for cap_key, st in sorted(capability_states(conn, user_id).items())
    ]
    parts = governor.month_spend_parts(conn, user_id, as_of.isoformat()[:7], cfg)
    return BriefAutonomy(
        enabled=cfg.autonomy.enabled,
        halted=governor.is_halted(conn, user_id),
        month_spend_usd=sum(parts),
        month_actions_usd=parts[0],
        month_listing_fees_usd=parts[1],
        month_llm_usd=llm_ledger.monthly_spend(conn, user_id, as_of.isoformat()[:7]),
        monthly_spend_cap_usd=cfg.autonomy.monthly_spend_cap_usd,
        ladder=ladder,
    )


def generate_brief(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> Brief:
    as_of = as_of or datetime.now(UTC).date()
    sections = cfg.brief_sections

    revenue = (
        analytics.revenue_window(conn, user_id, cfg, as_of=as_of) if sections.revenue else None
    )
    top_sellers = analytics.top_sellers(conn, user_id, cfg, as_of=as_of) if sections.selling else []
    viewed_not_sold = analytics.viewed_not_sold(conn, user_id) if sections.selling else []
    product_type_breakdown = (
        analytics.product_type_breakdown(conn, user_id, cfg, as_of=as_of)
        if sections.selling
        else []
    )
    size_breakdown = (
        analytics.size_breakdown(conn, user_id, cfg, as_of=as_of) if sections.selling else []
    )
    dead = analytics.dead_listings(conn, user_id, cfg, as_of=as_of) if sections.dying else []
    trend = analytics.trending(conn, user_id, cfg, as_of=as_of) if sections.dying else []
    shoot = analytics.shoot_more(conn, user_id, cfg, as_of=as_of) if sections.shoot_more else []
    notes = (
        analytics.data_quality_notes(conn, user_id, cfg, as_of=as_of)
        if sections.data_quality
        else []
    )

    needs_you, queued = _split_pending(conn, user_id, cfg, as_of) if sections.autonomy else ([], [])
    readback_failed = _readback_failed(conn, user_id) if sections.autonomy else []
    done_recent = _done_recent(conn, user_id, as_of) if sections.autonomy else []
    refused_recent = _refused_recent(conn, user_id, as_of) if sections.autonomy else []
    autonomy = _autonomy_section(conn, user_id, cfg, as_of) if sections.autonomy else None
    caption_drafts = _caption_drafts(conn, user_id, as_of) if sections.captions else []
    pin_drafts = _pin_drafts(conn, user_id, as_of) if sections.pins else []
    pin_experiments = (
        analytics.pin_experiment_readout(conn, user_id, cfg, as_of=as_of)
        if sections.pin_experiments
        else []
    )

    social_queue = _social_queue(conn, user_id, cfg, as_of) if sections.social_queue else None
    scorecard = build_scorecard(conn, user_id, cfg, as_of) if sections.scorecard else None
    outcome_list = outcomes.action_outcomes(conn, user_id, cfg, as_of) if sections.outcomes else []
    rollup_days = cfg.outcomes.rollup_window_days
    best, worst = outcomes.best_and_worst(outcome_list, rollup_days, cfg.outcomes.top_n)

    return Brief(
        generated_at=as_of,
        window_days=cfg.windows.revenue_window_days,
        revenue=revenue,
        top_sellers=top_sellers,
        viewed_not_sold=viewed_not_sold,
        dead_listings=dead,
        trending=trend,
        product_type_breakdown=product_type_breakdown,
        size_breakdown=size_breakdown,
        shoot_more=shoot,
        data_quality_notes=notes,
        needs_you=needs_you,
        queued=queued,
        readback_failed=readback_failed,
        done_recent=done_recent,
        refused_recent=refused_recent,
        autonomy=autonomy,
        caption_drafts=caption_drafts,
        pin_drafts=pin_drafts,
        pin_experiments=pin_experiments,
        social_queue=social_queue,
        scorecard=scorecard,
        outcome_rollups=[
            r for n in cfg.outcomes.windows_days for r in outcomes.rollup(outcome_list, n)
        ],
        outcome_best=best,
        outcome_worst=worst,
        outcomes_total=len(outcome_list),
    )


def _money(usd: float) -> str:
    return f"${usd:,.2f}"


def _pct(cur: float, prior: float) -> str:
    return "n/a" if prior == 0 else f"{(cur - prior) / prior:+.0%}"


def _render_scorecard(sc: Scorecard) -> list[str]:
    lines = ["", "THE NUMBERS (contribution profit; item revenue, trailing windows)"]
    for w in sc.windows:
        unknown = (
            f" [+{w.pod_units_cost_unknown} POD units, cost unknown]"
            if w.pod_units_cost_unknown
            else ""
        )
        lines.append(
            f"  {w.days:>3}d: revenue {_money(w.revenue_usd)} | "
            f"txn fee {_money(w.transaction_fees_usd)}"
            f" | processing {_money(w.processing_fees_usd)} | listing/renewal fees "
            f"{_money(w.listing_fees_usd)} | fulfillment {_money(w.fulfillment_usd)}{unknown}"
            f" | ads {_money(w.ad_spend_usd)} => CONTRIBUTION {_money(w.contribution_usd)} "
            f"({w.units} units, {w.orders} orders)"
        )
    t = sc.traffic
    n = t.window_days
    ctx = "market/seasonality context, not shop performance"
    if t.listings_covered:
        lines.append(
            f"  Shop views last {n}d vs prior {n}d ({ctx}): "
            f"{t.views_current:g} vs {t.views_prior:g} ({_pct(t.views_current, t.views_prior)}); "
            f"favorites {t.favorites_current:g} vs {t.favorites_prior:g} "
            f"({_pct(t.favorites_current, t.favorites_prior)}) -- "
            f"{t.listings_covered} of {t.listings_observed} listings covered"
        )
    elif t.views_current_only is not None:
        lines.append(
            f"  Shop views last {n}d ({ctx}): {t.views_current_only:g} over "
            f"{t.listings_current_only} listings; prior {n}d not measurable yet"
        )
    else:
        lines.append(f"  Shop views last {n}d vs prior {n}d ({ctx}): not measurable yet")
    lines.extend(f"  note: {note}" for note in sc.notes)
    return lines


def _fmt_change(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}"


def _outcome_line(o: ActionOutcome, days: int) -> str:
    w = outcomes.window_of(o, days)
    head = f"  {o.capability} on {o.title} ({o.anchor}) {days}d: "
    if w is None:
        return head + "not measurable"
    if w.status != "measured":
        return head + w.status
    if w.baseline == "new listing":
        return head + (
            f"new listing, {w.views_after_per_day:.2f} views/day after, "
            f"{w.sales_after} sold (no before)"
        )
    raw = _fmt_change(w.views_change_raw)
    adj = _fmt_change(w.views_change_adjusted) if w.baseline == "ok" else "no baseline"
    return head + (
        f"views/day {raw} raw, {adj} vs shop trend ({w.controls_n} controls); "
        f"sales {w.sales_before}->{w.sales_after}"
    )


def _render_outcomes(brief: Brief) -> list[str]:
    lines = [
        "",
        f"OUTCOMES ({brief.outcomes_total} executed actions; correlational, small samples, "
        "changes in views/day are relative to the shop-wide trend over the same window)",
    ]
    for r in brief.outcome_rollups:
        lines.append(
            f"  {r.capability} {r.days}d: n={r.n_actions}, adjusted n={r.n_measured}"
            f" (no baseline {r.n_no_baseline}; not yet measurable {r.n_not_yet_measurable}; "
            f"not measurable {r.n_not_measurable}), median adjusted views/day "
            f"{_fmt_change(r.median_adjusted_views_per_day)} (raw "
            f"{_fmt_change(r.median_raw_views_per_day)}), sold after {r.sales_after}"
        )
    days = brief.outcome_rollups[0].days if brief.outcome_rollups else 7
    if brief.outcome_best:
        lines.append(f"  Top {len(brief.outcome_best)} ({days}d, adjusted):")
        lines.extend("  " + _outcome_line(o, days) for o in brief.outcome_best)
    if brief.outcome_worst:
        lines.append(f"  Bottom {len(brief.outcome_worst)} ({days}d, adjusted):")
        lines.extend("  " + _outcome_line(o, days) for o in brief.outcome_worst)
    return lines


def render_text(brief: Brief) -> str:
    lines = [f"ShopSteward -- {brief.generated_at.isoformat()}"]

    # NEEDS YOU / DONE / REFUSED / AUTONOMY -- draft §6, PR3. brief.autonomy
    # is None iff cfg.brief_sections.autonomy is False; the whole chassis
    # bundle is omitted together, never shown half-populated.
    if brief.autonomy is not None:
        lines.append("")
        lines.append(f"NEEDS YOU ({len(brief.needs_you) + len(brief.readback_failed)})")
        for p in brief.needs_you:
            lines.append(
                f"  [{p.action_id}] {p.capability} -> {p.target_type}:{p.target_id} : "
                f"{p.reason} (expires {p.expires_at})"
            )
        for f in brief.readback_failed:
            lines.append(
                f"  DRAFT NOT PUBLISHED (read-back failed) {f.draft_id} "
                f"(etsy {f.etsy_listing_id}) {f.title or ''}: " + "; ".join(f.problems)
            )

        lines.append("")
        lines.append(f"QUEUED (next run) -- no action needed from you ({len(brief.queued)})")
        for q in brief.queued:
            state = "WILL RUN" if q.will_execute else "BLOCKED"
            lines.append(
                f"  [{q.action_id}] {q.capability} -> {q.target_type}:{q.target_id} : "
                f"{state}: {q.verdict}"
            )

        lines.append("")
        lines.append(f"DONE ({len(brief.done_recent)})")
        for a in brief.done_recent:
            undo = f" -- undo: ops undo {a.action_id}" if a.undo_available else ""
            lines.append(f"  [{a.action_id}] {a.capability} -> {a.target_id} : {a.reason}{undo}")

        lines.append("")
        lines.append(f"REFUSED ({len(brief.refused_recent)})")
        for r in brief.refused_recent:
            lines.append(f"  {r.capability} -> {r.target_id} : {r.reason}")

    sq = brief.social_queue
    if sq is not None and (sq.queued or sq.unresolved or sq.errors or sq.deficits):
        lines.append("")
        lines.append(f"SOCIAL QUEUE ({len(sq.queued)} queued in Buffer)")
        # Deficits and unresolved intents first: they are what needs attention.
        for u in sq.unresolved:
            lines.append(
                f"  UNRESOLVED [{u.action_id}] {u.channel} listing {u.listing_id} drafted "
                f"{u.drafted_at}: create outcome unknown, listing blocked until buffer-sync "
                "resolves it"
            )
        for err in sq.errors:
            lines.append(f"  ERROR {err}")
        for channel, owed in sorted(sq.deficits.items()):
            lines.append(f"  {channel}: {owed} post(s) owed this week")
        for q in sq.queued:
            note = (
                " -- posted despite a refusal, delete via Buffer/CLI if unwanted"
                if q.reconciled
                else ""
            )
            lines.append(
                f"  [{q.channel}] listing {q.listing_id} -- due {q.due_at}, undo until "
                f"{q.undo_until} (ops undo {q.action_id}){note}"
            )

    if brief.caption_drafts:
        lines.append("")
        lines.append(f"CAPTIONS TO POST (copy to IG/FB) ({len(brief.caption_drafts)})")
        for c in brief.caption_drafts:
            mark_posted = f" -- mark posted: ops mark-posted {c.action_id}" if c.action_id else ""
            lines.append(f'  [{c.channel}] {c.title} -- "{c.caption}"{mark_posted}')

    if brief.pin_drafts:
        lines.append("")
        lines.append(f"PINS TO POST (copy to Pinterest) ({len(brief.pin_drafts)})")
        for p in brief.pin_drafts:
            mark_posted = f" -- mark posted: ops mark-posted {p.action_id}" if p.action_id else ""
            lines.append(
                f'  {p.title} -- board "{p.board_key}" -- {p.destination_url} -- '
                f'"{p.description}"{mark_posted}'
            )

    if brief.pin_experiments:
        lines.append("")
        lines.append(
            f"PIN EXPERIMENTS -- correlational only, NOT proof the pin caused it "
            f"({len(brief.pin_experiments)})"
        )
        for x in brief.pin_experiments:
            if x.observed_views_per_day is None and x.days_since_posted < x.window_days:
                lines.append(
                    f"  {x.title} -- posted {x.days_since_posted}d ago -- too early to measure"
                )
            elif x.observed_views_per_day is None:
                # Window elapsed but the synced view history doesn't span it
                # (analytics._views_rate_per_day): absence, not "too early".
                lines.append(
                    f"  {x.title} -- posted {x.days_since_posted}d ago -- not measurable: "
                    f"synced views don't cover the {x.window_days} days after posting"
                )
            elif x.baseline_views_per_day is None:
                lines.append(
                    f"  {x.title} -- posted {x.days_since_posted}d ago -- "
                    f"{x.observed_views_per_day:.1f} views/day since, no prior baseline "
                    "(listing too new)"
                )
            else:
                lines.append(
                    f"  {x.title} -- posted {x.days_since_posted}d ago -- "
                    f"{x.baseline_views_per_day:.1f} -> {x.observed_views_per_day:.1f} views/day "
                    f"({x.delta_views_per_day:+.1f}) -- may be pin-driven, may be coincidental"
                )
            if x.buffer_metrics is not None:
                # An absent key is "not reported", never 0 (design §6).
                shown = ", ".join(
                    f"{k}: {x.buffer_metrics[k]:g}"
                    if k in x.buffer_metrics
                    else f"{k}: not yet reported"
                    for k in _BUFFER_METRIC_KEYS
                )
                lines.append(f"    Buffer: {shown}")

    if brief.scorecard is not None:
        lines.extend(_render_scorecard(brief.scorecard))

    if brief.outcomes_total or brief.outcome_rollups:
        lines.extend(_render_outcomes(brief))

    lines.append("")
    lines.append("THE SHOP")

    if brief.revenue is not None:
        r = brief.revenue
        pct = (
            "n/a (no revenue in the prior window)"
            if r.pct_change is None
            else f"{r.pct_change:+.1%}"
        )
        lines.append(
            f"  Revenue ({r.window_days}d, receipt total incl. shipping/tax): "
            f"{_money(r.current_usd)} ({pct} vs prior {r.window_days}d) -- "
            f"{r.orders} orders, {r.units} units"
        )

    lines.append("  Selling (item revenue only -- excludes shipping/tax):")
    if brief.top_sellers:
        for s in brief.top_sellers:
            lines.append(f"    {s.title} -- {s.units} units, {_money(s.revenue_usd)}")
    else:
        lines.append("    (nothing sold this window)")

    lines.append("  Product mix (item revenue only -- excludes shipping/tax):")
    if brief.product_type_breakdown:
        for p in brief.product_type_breakdown:
            lines.append(
                f"    {p.product_type}: {p.listing_count} listings, {p.units} units, "
                f"{_money(p.revenue_usd)}"
            )
    else:
        lines.append("    (no listings observed yet)")

    lines.append(
        "  By size (best-effort, from title text only, item revenue only -- "
        "see data quality below):"
    )
    if brief.size_breakdown:
        for sz in brief.size_breakdown:
            label = sz.size if sz.size is not None else "(no size signal)"
            lines.append(f"    {label}: {sz.units} units, {_money(sz.revenue_usd)}")
    else:
        lines.append("    (nothing sold this window)")

    lines.append(f"  Viewed but never sold ({len(brief.viewed_not_sold)}):")
    for v in brief.viewed_not_sold:
        lines.append(f"    {v.title} -- {v.views_lifetime} lifetime views, 0 sales")

    n_dead = len(brief.dead_listings)
    lines.append(f"  Dying, candidates only -- hand-set thresholds, not an insight ({n_dead}):")
    for d in brief.dead_listings:
        lines.append(f"    {d.title} -- 0 views/sales in the window, observed {d.days_observed}d")

    lines.append(f"  Trending ({len(brief.trending)}):")
    for t in brief.trending:
        lines.append(
            f"    {t.title} -- {t.views_recent} views this window vs {t.views_prior} prior"
        )

    n_shoot = len(brief.shoot_more)
    lines.append(
        f"  Shoot more of -- product types selling well on few listings, not photo "
        f"subjects ({n_shoot}):"
    )
    for s in brief.shoot_more:
        lines.append(
            f"    {s.product_type} -- {s.listing_count} listings, "
            f"{_money(s.revenue_usd)} item revenue this window"
        )

    if brief.data_quality_notes:
        lines.append("  Data quality:")
        for note in brief.data_quality_notes:
            lines.append(f"    - {note}")

    if brief.autonomy is not None:
        a = brief.autonomy
        state = "halted" if a.halted else ("ON" if a.enabled else "OFF")
        lines.append("")
        lines.append("AUTONOMY")
        lines.append(f"  autonomy: {state}")
        lines.append(
            f"  month spend: {_money(a.month_spend_usd)} of {_money(a.monthly_spend_cap_usd)} cap "
            f"(actions {_money(a.month_actions_usd)}, listing fees "
            f"{_money(a.month_listing_fees_usd)}); LLM {_money(a.month_llm_usd)} (separate ledger)"
        )
        for row in a.ladder:
            lines.append(
                f"    {row.capability}  T{int(row.tier)}  ({row.approvals} approvals/"
                f"{row.rejections} rejections, {row.executions} executions, "
                f"since {row.tier_since})"
            )

    return "\n".join(lines)
