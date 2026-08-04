"""Assemble + render the read-only shop brief (design §6's "THE SHOP"
section only). §6 also has NEEDS YOU / DONE OVERNIGHT / REFUSED / AUTONOMY
sections -- those need the registry/governor/runner/ladder, which are M8a
slices 2+ and are not built here (task instructions, design §9 slice 1).

generate_brief() is deterministic SQL via analytics.py (PURE, no LLM, no
network -- design §7). render_text() is a template, not a model."""

import sqlite3
from datetime import UTC, date, datetime

from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.models import Brief, OpsConfig


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
    )


def _money(usd: float) -> str:
    return f"${usd:,.2f}"


def render_text(brief: Brief) -> str:
    lines = [f"ShopSteward -- {brief.generated_at.isoformat()}", "", "THE SHOP"]

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

    return "\n".join(lines)
