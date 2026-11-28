"""THE NUMBERS -- the brief's contribution-profit scorecard. PURE: events and
projections only, no network, no LLM.

contribution = item revenue - transaction fee - payment processing - listing and
renewal fees actually spent - POD fulfillment - attributed ad spend.

What is and is not known, stated rather than guessed:
- Revenue is `proj_sale_items` (item price x quantity). Etsy also charges its
  fees on shipping/tax; those are not in the item rows, so the fees here are
  slightly UNDER-stated.
- Listing/renewal fees come from the governor's own spend ledger
  (`governor.spend_parts_between`): only what ShopSteward itself spent, since
  the event log began. Fees paid by hand are invisible.
- POD fulfillment: `proj_sale_items` carries no variant, so a POD sale is matched
  to the nearest-priced variant of its linked draft (`proj_listing_drafts.
  variants_json`, whose `base_cost` came from pod.json at pricing time). A sale
  whose cost cannot be resolved is counted in `pod_units_cost_unknown`, never
  priced at 0 silently.
- Offsite Ads (12-15% of an ad-driven sale) and attributed ad spend are not
  recorded anywhere: ad spend is 0 and offsite ads are NOT deducted."""

import json
import sqlite3
from datetime import date, timedelta

from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import analytics, governor
from shopsteward.pipeline.ops.models import (
    OpsConfig,
    Scorecard,
    ScorecardTraffic,
    ScorecardWindow,
)


def _pod_listing_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    return {
        int(e.payload["etsy_listing_id"])
        for e in read_all(conn, "listingdraft.provider_linked")
        if e.user_id == user_id and e.payload.get("etsy_listing_id") is not None
    }


def _priced_variants(conn: sqlite3.Connection, user_id: int) -> dict[int, list[dict]]:
    """etsy listing id -> priced POD variants ({retail_price, base_cost, shipping_est})."""
    try:
        rows = conn.execute(
            "SELECT etsy_listing_id, variants_json FROM proj_listing_drafts "
            "WHERE user_id=? AND pod_config_hash IS NOT NULL AND etsy_listing_id IS NOT NULL",
            (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:  # listings projection not built in this conn
        return {}
    out: dict[int, list[dict]] = {}
    for r in rows:
        variants = [
            v
            for v in json.loads(r["variants_json"] or "[]")
            if v.get("base_cost") is not None and v.get("retail_price") is not None
        ]
        if variants:
            out.setdefault(int(r["etsy_listing_id"]), []).extend(variants)
    return out


def _fulfillment(
    conn: sqlite3.Connection, user_id: int, start: date, end: date
) -> tuple[float, int, bool]:
    """(cost of POD units sold in the window, units with unresolved cost,
    whether any cost was matched among several variants)."""
    pod_ids = _pod_listing_ids(conn, user_id)
    if not pod_ids:
        return 0.0, 0, False
    variants_by_listing = _priced_variants(conn, user_id)
    total, unknown, nearest_used = 0.0, 0, False
    for r in conn.execute(
        "SELECT listing_id, quantity, price_usd FROM proj_sale_items "
        "WHERE user_id=? AND sale_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall():
        if r["listing_id"] not in pod_ids:
            continue
        variants = variants_by_listing.get(r["listing_id"])
        if not variants:
            unknown += r["quantity"]
            continue
        best = min(variants, key=lambda v: abs(v["retail_price"] - r["price_usd"]))
        nearest_used = nearest_used or len(variants) > 1
        total += r["quantity"] * (best["base_cost"] + (best.get("shipping_est") or 0.0))
    return round(total, 2), unknown, nearest_used


def _window(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, days: int, as_of: date
) -> tuple[ScorecardWindow, bool]:
    start = as_of - timedelta(days=days - 1)
    revenue, units, orders = conn.execute(
        "SELECT COALESCE(SUM(quantity*price_usd),0), COALESCE(SUM(quantity),0), "
        "COUNT(DISTINCT receipt_id) FROM proj_sale_items "
        "WHERE user_id=? AND sale_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), as_of.isoformat()),
    ).fetchone()
    eco = cfg.economics
    transaction = revenue * eco.transaction_fee_pct
    processing = revenue * eco.payment_processing_pct + orders * eco.payment_processing_fixed_usd
    actions_usd, listing_fees = governor.spend_parts_between(conn, user_id, start, as_of, cfg)
    fees_spent = actions_usd + listing_fees
    fulfillment, unknown, nearest = _fulfillment(conn, user_id, start, as_of)
    contribution = revenue - transaction - processing - fees_spent - fulfillment
    return (
        ScorecardWindow(
            days=days,
            revenue_usd=round(revenue, 2),
            orders=orders,
            units=units,
            transaction_fees_usd=round(transaction, 2),
            processing_fees_usd=round(processing, 2),
            listing_fees_usd=round(fees_spent, 2),
            fulfillment_usd=fulfillment,
            contribution_usd=round(contribution, 2),
            pod_units_cost_unknown=unknown,
        ),
        nearest,
    )


def _traffic(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> ScorecardTraffic:
    n = cfg.economics.traffic_window_days
    (end_str,) = conn.execute(
        "SELECT MAX(day) FROM proj_listing_daily WHERE user_id=? AND day<=?",
        (user_id, as_of.isoformat()),
    ).fetchone()
    if end_str is None:
        return ScorecardTraffic(
            window_days=n, end_day=None, listings_observed=0, listings_covered=0
        )
    end = date.fromisoformat(end_str)
    cur = (end - timedelta(days=n - 1), end)
    prior_end = cur[0] - timedelta(days=1)
    prior = (prior_end - timedelta(days=n - 1), prior_end)
    listing_ids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT listing_id FROM proj_listing_daily WHERE user_id=? AND day<=?",
            (user_id, end_str),
        )
    ]
    views_cur: dict[int, float] = {}
    views_prior: dict[int, float] = {}
    fav_cur: dict[int, float] = {}
    fav_prior: dict[int, float] = {}
    for lid in listing_ids:
        for store, window, counter in (
            (views_cur, cur, "views"),
            (views_prior, prior, "views"),
            (fav_cur, cur, "num_favorers"),
            (fav_prior, prior, "num_favorers"),
        ):
            rate = analytics.window_rate_per_day(conn, user_id, lid, window[0], window[1], counter)
            if rate is not None:
                store[lid] = rate * n  # back to a window total
    both = sorted(views_cur.keys() & views_prior.keys() & fav_cur.keys() & fav_prior.keys())
    traffic = ScorecardTraffic(
        window_days=n,
        end_day=end_str,
        listings_observed=len(listing_ids),
        listings_covered=len(both),
        views_current_only=round(sum(views_cur.values()), 1) if views_cur else None,
        listings_current_only=len(views_cur),
    )
    if both:
        traffic.views_current = round(sum(views_cur[i] for i in both), 1)
        traffic.views_prior = round(sum(views_prior[i] for i in both), 1)
        traffic.favorites_current = round(sum(fav_cur[i] for i in both), 1)
        traffic.favorites_prior = round(sum(fav_prior[i] for i in both), 1)
    return traffic


def build_scorecard(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> Scorecard:
    windows: list[ScorecardWindow] = []
    nearest_any = False
    for days in cfg.economics.windows_days:
        w, nearest = _window(conn, user_id, cfg, days, as_of)
        windows.append(w)
        nearest_any = nearest_any or nearest
    traffic = _traffic(conn, user_id, cfg, as_of)

    notes = [
        "Fees are computed on item revenue (shipping/tax excluded), so slightly under-stated; "
        "offsite ads and ad spend are not recorded (not deducted).",
        "Listing/renewal fees are only those ShopSteward itself spent (governor ledger).",
    ]
    if any(w.pod_units_cost_unknown for w in windows):
        notes.append("Some POD units have no resolvable cost: fulfillment is UNDER-stated there.")
    if nearest_any:
        notes.append(
            "POD cost matched to the nearest-priced variant of the listing (no SKU on sales)."
        )
    first_obs = conn.execute(
        "SELECT MIN(day) FROM proj_listing_daily WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    if traffic.end_day is None:
        notes.append("Traffic: no listing snapshots yet.")
    elif traffic.listings_covered == 0:
        n = traffic.window_days
        notes.append(
            f"Traffic: too sparse for {n}d vs prior {n}d -- needs daily listing snapshots "
            f"spanning {2 * n} days (snapshots since {first_obs}, latest {traffic.end_day})."
        )
    elif traffic.listings_covered < traffic.listings_observed:
        notes.append(
            f"Traffic covers {traffic.listings_covered} of {traffic.listings_observed} "
            "observed listings (the rest lack snapshots across both windows)."
        )
    return Scorecard(windows=windows, traffic=traffic, notes=notes)
