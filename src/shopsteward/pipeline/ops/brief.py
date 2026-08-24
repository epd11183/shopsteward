"""Assemble + render the read-only shop brief (design §6's "THE SHOP"
section, plus the NEEDS YOU / DONE / REFUSED / AUTONOMY chassis sections
added in PR3, M8a spec §8 PR3 / draft §6). The chassis sections read
proj_actions/proj_capability_state + governor.month_spend()/is_halted() --
still PURE, no LLM, no network, exactly like THE SHOP's analytics.py reads.

generate_brief() is deterministic (PURE, no LLM, no network -- design §7).
render_text() is a template, not a model."""

import sqlite3
from datetime import UTC, date, datetime

from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import analytics, governor
from shopsteward.pipeline.ops.models import (
    Brief,
    BriefAction,
    BriefAutonomy,
    BriefCaption,
    BriefLadderRow,
    BriefPin,
    BriefProposal,
    BriefRefusal,
    OpsConfig,
    ProposedAction,
    Tier,
)
from shopsteward.pipeline.ops.projections import (
    _action_id_from_destination_url,
    action_rows,
    capability_states,
)

# ponytail: fixed 7-day lookback for DONE/REFUSED, not a config knob -- add
# one under cfg.windows if the operator ever wants a longer/shorter Brief
# history.
_DONE_REFUSED_WINDOW_DAYS = 7


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


def _needs_you(conn: sqlite3.Connection, user_id: int, as_of: date) -> list[BriefProposal]:
    proposed = _proposed_by_action_id(conn, user_id)
    out = []
    for row in action_rows(conn):
        if row["user_id"] != user_id or row["state"] != "proposed":
            continue
        p = proposed.get(row["action_id"])
        if p is None or as_of > date.fromisoformat(p.expires_at):
            continue  # never proposed (shouldn't happen) or expired
        out.append(
            BriefProposal(
                action_id=row["action_id"],
                capability=row["capability"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                tier=Tier(row["tier"]),
                reason=row["reason"],
                expires_at=p.expires_at,
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
    yet, same ponytail as _DONE_REFUSED_WINDOW_DAYS above)."""
    out = []
    for e in read_all(conn, "social.caption_drafted"):
        if e.user_id != user_id or not _within_window(
            e.created_at, as_of, _DONE_REFUSED_WINDOW_DAYS
        ):
            continue
        out.append(
            BriefCaption(
                listing_id=e.payload["listing_id"],
                title=e.payload["title"],
                caption=e.payload["caption"],
                drafted_at=e.payload["drafted_at"],
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


def _autonomy_section(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> BriefAutonomy:
    ladder = [
        BriefLadderRow(
            capability=cap_key,
            tier=st.tier,
            approvals=st.approvals,
            rejections=st.rejections,
            executions=st.executions,
            undos=st.undos,
            tier_since=st.tier_since,
        )
        for cap_key, st in sorted(capability_states(conn, user_id).items())
    ]
    return BriefAutonomy(
        enabled=cfg.autonomy.enabled,
        halted=governor.is_halted(conn, user_id),
        month_spend_usd=governor.month_spend(conn, user_id, as_of.isoformat()[:7]),
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

    needs_you = _needs_you(conn, user_id, as_of) if sections.autonomy else []
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
        done_recent=done_recent,
        refused_recent=refused_recent,
        autonomy=autonomy,
        caption_drafts=caption_drafts,
        pin_drafts=pin_drafts,
        pin_experiments=pin_experiments,
    )


def _money(usd: float) -> str:
    return f"${usd:,.2f}"


def render_text(brief: Brief) -> str:
    lines = [f"ShopSteward -- {brief.generated_at.isoformat()}"]

    # NEEDS YOU / DONE / REFUSED / AUTONOMY -- draft §6, PR3. brief.autonomy
    # is None iff cfg.brief_sections.autonomy is False; the whole chassis
    # bundle is omitted together, never shown half-populated.
    if brief.autonomy is not None:
        lines.append("")
        lines.append(f"NEEDS YOU ({len(brief.needs_you)})")
        for p in brief.needs_you:
            lines.append(
                f"  [{p.action_id}] {p.capability} -> {p.target_type}:{p.target_id} : "
                f"{p.reason} (expires {p.expires_at})"
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

    if brief.caption_drafts:
        lines.append("")
        lines.append(f"CAPTIONS TO POST (copy to IG/FB) ({len(brief.caption_drafts)})")
        for c in brief.caption_drafts:
            lines.append(f'  {c.title} -- "{c.caption}"')

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
            if x.observed_views_per_day is None:
                lines.append(
                    f"  {x.title} -- posted {x.days_since_posted}d ago -- too early to measure"
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
            f"  spend {_money(a.month_spend_usd)} of {_money(a.monthly_spend_cap_usd)} cap"
        )
        for row in a.ladder:
            lines.append(
                f"    {row.capability}  T{int(row.tier)}  ({row.approvals} approvals/"
                f"{row.rejections} rejections, {row.executions} executions, "
                f"since {row.tier_since})"
            )

    return "\n".join(lines)
