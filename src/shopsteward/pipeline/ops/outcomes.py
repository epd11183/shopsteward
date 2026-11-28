"""Per-action outcome readout: for every executed listing action, the target
listing's views/favorites per day and sales over the N days before vs after it,
from `proj_listing_daily` / `proj_sale_items`. PURE (events + projections).

Same honesty rules as the pin readout (`analytics.pin_experiment_readout`):
- "not yet measurable": the after-window has not elapsed;
- "not measurable": it has, but the synced snapshots do not cover it
  (`analytics._views_rate_per_day`'s coverage rule) -- absence, never 0.

Market adjustment: a shop-wide slump would otherwise be blamed on every action.
Each raw change in views/day is therefore ALSO reported net of the median change
over the same windows across CONTROL listings (active listings with no executed
action in [anchor-N, anchor+N]); fewer than `outcomes.min_controls` controls =>
"no baseline" and no adjusted figure. Correlational only; with this few actions
the roll-ups are raw numbers with n, never a significance claim."""

import json
import sqlite3
import statistics
from datetime import date, timedelta

from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops.models import (
    ActionOutcome,
    OpsConfig,
    OutcomeRollup,
    OutcomeWindow,
)
from shopsteward.pipeline.ops.projections import action_rows

_NEW_LISTING_CAPABILITIES = ("listing.catalog_expand", "listing.gapfill_reprint")
_MEASURED_CAPABILITIES = frozenset(
    {
        "listing.seo_edit",
        "listing.size_claims_fix",
        "listing.reprice",
        "listing.deactivate",
        "listing.renew",
        *_NEW_LISTING_CAPABILITIES,
    }
)


def _in_scope(capability: str) -> bool:
    return capability in _MEASURED_CAPABILITIES or capability.startswith("social.")


def _target_listing(row: dict) -> int | None:
    """The listing an action is about: a new-listing capability's own `after`
    carries the CREATED listing; `social.caption_draft` targets "listing:channel";
    gapfill's target_id is "listing:type" but the new listing is in `after`."""
    if row["capability"] in _NEW_LISTING_CAPABILITIES:
        lid = (json.loads(row["after_json"] or "{}") or {}).get("etsy_listing_id")
        return int(lid) if lid is not None else None
    head = str(row["target_id"]).split(":", 1)[0]
    return int(head) if head.isdigit() else None


def _anchor(row: dict, due_by_action: dict[str, str]) -> date:
    """When the action took effect: execution day, or for a Buffer post its
    scheduled `due_at` (queuing a post is not publishing it)."""
    stamp = due_by_action.get(row["action_id"]) or row["resolved_at"]
    return date.fromisoformat(stamp[:10])


def _units_sold(
    conn: sqlite3.Connection, user_id: int, listing_id: int, start: date, end: date
) -> int:
    (n,) = conn.execute(
        "SELECT COALESCE(SUM(quantity),0) FROM proj_sale_items "
        "WHERE user_id=? AND listing_id=? AND sale_date BETWEEN ? AND ?",
        (user_id, listing_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    return n


def _control_median_change(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    anchor: date,
    n: int,
    as_of: date,
    exclude: int | None,
    active_ids: list[int],
    acted: dict[int, list[date]],
) -> tuple[float | None, int]:
    """(median change in views/day across control listings, how many qualified)."""
    lo, hi = anchor - timedelta(days=n), anchor + timedelta(days=n)
    changes: list[float] = []
    for lid in active_ids:
        if lid == exclude or any(lo <= d <= hi for d in acted.get(lid, ())):
            continue
        before, after = analytics._before_after_views_per_day(conn, user_id, lid, anchor, n, as_of)
        if before is not None and after is not None:
            changes.append(after - before)
    if len(changes) < cfg.outcomes.min_controls:
        return None, len(changes)
    return statistics.median(changes), len(changes)


def _window(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    listing_id: int,
    capability: str,
    anchor: date,
    n: int,
    as_of: date,
    active_ids: list[int],
    acted: dict[int, list[date]],
) -> OutcomeWindow:
    if as_of < anchor + timedelta(days=n):
        return OutcomeWindow(days=n, status="not yet measurable")
    views_b, views_a = analytics._before_after_views_per_day(
        conn, user_id, listing_id, anchor, n, as_of
    )
    fav_b, fav_a = analytics._before_after_views_per_day(
        conn, user_id, listing_id, anchor, n, as_of, counter="num_favorers"
    )
    sales_after = _units_sold(
        conn, user_id, listing_id, anchor + timedelta(days=1), anchor + timedelta(days=n)
    )
    sales_before = _units_sold(
        conn, user_id, listing_id, anchor - timedelta(days=n), anchor - timedelta(days=1)
    )
    win = OutcomeWindow(
        days=n,
        status="measured",
        views_before_per_day=views_b,
        views_after_per_day=views_a,
        favorites_before_per_day=fav_b,
        favorites_after_per_day=fav_a,
        sales_before=sales_before,
        sales_after=sales_after,
    )
    if views_a is None:
        win.status = "not measurable"
        return win
    if capability in _NEW_LISTING_CAPABILITIES:
        win.baseline = "new listing"  # created by the action: after-only, no 'before'
        return win
    if views_b is None:
        win.status = "no before data"
        win.baseline = "no baseline"
        return win
    win.views_change_raw = views_a - views_b
    median_change, controls = _control_median_change(
        conn, user_id, cfg, anchor, n, as_of, listing_id, active_ids, acted
    )
    win.controls_n = controls
    if median_change is None:
        win.baseline = "no baseline"
    else:
        win.control_median_change = median_change
        win.views_change_adjusted = win.views_change_raw - median_change
    return win


def action_outcomes(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> list[ActionOutcome]:
    rows = [
        r
        for r in action_rows(conn)
        if r["user_id"] == user_id
        and r["state"] == "executed"
        and r["resolved_at"]
        and _in_scope(r["capability"])
    ]
    due = {
        e.payload["action_id"]: e.payload["due_at"]
        for e in read_all(conn, "social.buffer_queued")
        if e.user_id == user_id and e.payload.get("due_at") and e.payload.get("action_id")
    }
    # Every executed action's (listing, day): excludes those listings from control sets.
    acted: dict[int, list[date]] = {}
    resolved: list[tuple[dict, int | None, date]] = []
    for r in rows:
        lid = _target_listing(r)
        day = _anchor(r, due)
        resolved.append((r, lid, day))
        if lid is not None:
            acted.setdefault(lid, []).append(day)
    active_ids = [
        x[0]
        for x in conn.execute(
            "SELECT listing_id FROM proj_listings WHERE user_id=? AND state='active'", (user_id,)
        )
    ]
    out: list[ActionOutcome] = []
    for r, lid, day in resolved:
        windows = (
            [
                _window(conn, user_id, cfg, lid, r["capability"], day, n, as_of, active_ids, acted)
                for n in cfg.outcomes.windows_days
            ]
            if lid is not None
            else [OutcomeWindow(days=n, status="not measurable") for n in cfg.outcomes.windows_days]
        )
        out.append(
            ActionOutcome(
                action_id=r["action_id"],
                capability=r["capability"],
                listing_id=lid,
                title=analytics._title(conn, user_id, lid) if lid is not None else r["target_id"],
                anchor=day.isoformat(),
                windows=windows,
            )
        )
    return out


def window_of(outcome: ActionOutcome, days: int) -> OutcomeWindow | None:
    return next((w for w in outcome.windows if w.days == days), None)


def rollup(outcomes: list[ActionOutcome], days: int) -> list[OutcomeRollup]:
    """Per capability over the `days` window: n, how many have an adjusted
    change, the median adjusted/raw change in views/day, units sold after."""
    by_cap: dict[str, list[OutcomeWindow | None]] = {}
    for o in outcomes:
        by_cap.setdefault(o.capability, []).append(window_of(o, days))
    out = []
    for cap, wins in sorted(by_cap.items()):
        measured = [w for w in wins if w is not None and w.status == "measured"]
        adjusted = [
            w.views_change_adjusted for w in measured if w.views_change_adjusted is not None
        ]
        raw = [w.views_change_raw for w in measured if w.views_change_raw is not None]
        out.append(
            OutcomeRollup(
                capability=cap,
                days=days,
                n_actions=len(wins),
                n_measured=len(adjusted),
                n_no_baseline=sum(1 for w in measured if w.baseline == "no baseline"),
                n_not_yet_measurable=sum(1 for w in wins if w and w.status == "not yet measurable"),
                n_not_measurable=sum(1 for w in wins if w is None or w.status == "not measurable"),
                median_adjusted_views_per_day=statistics.median(adjusted) if adjusted else None,
                median_raw_views_per_day=statistics.median(raw) if raw else None,
                sales_after=sum(w.sales_after or 0 for w in measured),
            )
        )
    return out


def best_and_worst(
    outcomes: list[ActionOutcome], days: int, top_n: int
) -> tuple[list[ActionOutcome], list[ActionOutcome]]:
    """Top and bottom `top_n` actions by adjusted change (only those that have one)."""

    def adj(o: ActionOutcome) -> float:
        w = window_of(o, days)
        return w.views_change_adjusted if w and w.views_change_adjusted is not None else 0.0

    scored = [
        o for o in outcomes if (w := window_of(o, days)) and w.views_change_adjusted is not None
    ]
    ranked = sorted(scored, key=adj, reverse=True)
    best = ranked[:top_n]
    worst = [o for o in reversed(ranked) if o not in best][:top_n]
    return best, worst


def planner_facts(outcomes: list[ActionOutcome], cfg: OpsConfig, limit: int = 10) -> dict:
    """The `action_outcomes` block of the planner facts: the per-capability roll-up
    (adjusted change only) plus the (capability, listing) pairs with a NEGATIVE
    adjusted change, so "avoid repeating on the same listing" is actionable."""
    days = cfg.outcomes.rollup_window_days
    negatives = []
    for o in outcomes:
        w = window_of(o, days)
        if w and w.views_change_adjusted is not None and w.views_change_adjusted < 0:
            negatives.append(
                {
                    "capability": o.capability,
                    "listing_id": o.listing_id,
                    "adjusted_views_per_day_change": round(w.views_change_adjusted, 2),
                }
            )
    negatives.sort(key=lambda d: d["adjusted_views_per_day_change"])
    return {
        "window_days": days,
        "by_capability": [
            {
                "capability": r.capability,
                "n_actions": r.n_actions,
                "n_measured": r.n_measured,
                "median_adjusted_views_per_day_change": (
                    None
                    if r.median_adjusted_views_per_day is None
                    else round(r.median_adjusted_views_per_day, 2)
                ),
                "sales_after": r.sales_after,
            }
            for r in rollup(outcomes, days)
        ],
        "negative_on_listing": negatives[:limit],
    }
