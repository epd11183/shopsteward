"""PURE functions over proj_listing_daily/proj_sale_items (this module) and
core's proj_listings/proj_sales (reused, not duplicated -- see
projections.py's docstring). Deterministic SQL and Python only: no LLM, no
network, no model of any kind (design §7/§9 slice 1 -- "what sold, what's
dead, what's trending is a GROUP BY, not inference").

Product-type classification is a config-driven keyword match against the
listing title (ops.json's product_type_keywords) -- never hardcoded, never
guessed beyond that. Size classification is a best-effort regex match for
an "NxM" pattern in the title; there is currently no reliable size signal
in this shop's data at all (physical print size lives in Etsy inventory
variations, which EtsyTransaction/proj_sale_items does not carry -- see
adapters/etsy/models.py's EtsyTransaction, which has no variation field).
Rows with no match are reported as size=None rather than guessed, and
data_quality_notes() calls out the coverage gap explicitly per the design's
instruction to say so rather than guess."""

import re
import sqlite3
from datetime import UTC, date, datetime, timedelta

from shopsteward.pipeline.ops.models import (
    DeadListing,
    ListingSales,
    OpsConfig,
    PinExperimentResult,
    ProductTypeStat,
    RevenueWindow,
    ShootMoreSuggestion,
    SizeStat,
    TrendingListing,
    ViewedNotSold,
)

_SIZE_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)", re.IGNORECASE)

# ponytail: cap by ROW COUNT (most-recent-N by drafted_at), not by a
# drafted_at day-window -- a day-window risks truncating a row whose own
# before/after measurement window (cfg.windows.revenue_window_days) hasn't
# finished yet, which would wrongly hide a "too early to measure" row rather
# than an already-measured one. A count cap avoids that in practice: rows
# most likely to still be unmeasured are the newest, and ORDER BY drafted_at
# DESC LIMIT keeps the newest first, so a too-early row is the LAST to be
# dropped, not the first -- correct as long as fewer than this many pins are
# ever drafted within a single measurement window (true today, given
# planner_max_per_capability_per_run: 1). Raise if the operator ever wants a
# longer history than this default -- add a windows.* config knob then.
_PIN_EXPERIMENTS_MAX_ROWS = 100


def _today(as_of: date | None) -> date:
    return as_of or datetime.now(UTC).date()


def _window(as_of: date, window_days: int) -> tuple[date, date]:
    end = as_of
    start = end - timedelta(days=window_days - 1)
    return start, end


def _title(conn: sqlite3.Connection, user_id: int, listing_id: int) -> str:
    row = conn.execute(
        "SELECT title FROM proj_listings WHERE user_id=? AND listing_id=?",
        (user_id, listing_id),
    ).fetchone()
    return row["title"] if row else f"listing {listing_id}"


def _views_delta(rows: list[sqlite3.Row]) -> int | None:
    """None means UNMEASURABLE: zero or one observation inside the window is
    not evidence of zero growth, it's an absence of data (a listing that
    fell off Etsy's active-only /listings/active, or a sync gap) -- callers
    must never treat None as 0.

    ponytail: for >=2 rows this still only compares the first/last row
    WITHIN the window (not the last row before it), so a sync gap
    straddling the window boundary can undercount a real gain. Upgrade to a
    true baseline lookup if that ever matters."""
    if len(rows) < 2:
        return None
    return rows[-1]["views"] - rows[0]["views"]


def _views_rate_per_day(rows: list[sqlite3.Row], window_days: int) -> float | None:
    """Like _views_delta, but a rate (divided by `window_days`) -- guarded
    against a sync gap distorting that division. `rows` must include a
    `day` column: the ACTUAL span between the first and last observation
    must cover nearly the whole window (>= window_days - 1, i.e. what two
    daily syncs exactly at the window's edges naturally produce) before a
    rate is returned at all -- e.g. 2 observations 3 days apart inside a
    7-day window must NOT be divided by 7 as if they spanned the full
    window (that would understate the true daily rate). Same "absence, not
    a distorted number" convention as _dead_listing_candidates's
    min_observed_days guard: too-thin coverage returns None, never a
    number computed from a gap."""
    delta = _views_delta(rows)
    if delta is None:
        return None
    span_days = (date.fromisoformat(rows[-1]["day"]) - date.fromisoformat(rows[0]["day"])).days
    if span_days < window_days - 1:
        return None
    return delta / window_days


def _classify_product_type(title: str, keywords: dict[str, list[str]]) -> str:
    lowered = title.lower()
    for product_type, substrings in keywords.items():
        if any(sub.lower() in lowered for sub in substrings):
            return product_type
    return "unknown"


def _extract_size(title: str) -> str | None:
    m = _SIZE_RE.search(title)
    if not m:
        return None
    return f"{m.group(1)}x{m.group(2)}"


def _sale_items_with_titles(
    conn: sqlite3.Connection, user_id: int, start: date, end: date
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT si.listing_id, si.quantity, si.price_usd, pl.title FROM proj_sale_items si "
        "LEFT JOIN proj_listings pl ON pl.user_id=si.user_id AND pl.listing_id=si.listing_id "
        "WHERE si.user_id=? AND si.sale_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()


def revenue_window(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> RevenueWindow:
    as_of = _today(as_of)
    window_days = cfg.windows.revenue_window_days
    start, end = _window(as_of, window_days)
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=window_days - 1)

    current_usd, orders = conn.execute(
        "SELECT COALESCE(SUM(total_usd),0), COUNT(*) FROM proj_sales "
        "WHERE user_id=? AND sale_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    (prior_usd,) = conn.execute(
        "SELECT COALESCE(SUM(total_usd),0) FROM proj_sales "
        "WHERE user_id=? AND sale_date BETWEEN ? AND ?",
        (user_id, prior_start.isoformat(), prior_end.isoformat()),
    ).fetchone()
    (units,) = conn.execute(
        "SELECT COALESCE(SUM(quantity),0) FROM proj_sale_items "
        "WHERE user_id=? AND sale_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchone()

    pct_change = None if prior_usd == 0 else (current_usd - prior_usd) / prior_usd
    return RevenueWindow(
        window_days=window_days,
        start=start,
        end=end,
        current_usd=round(current_usd, 2),
        prior_usd=round(prior_usd, 2),
        pct_change=pct_change,
        orders=orders,
        units=units,
    )


def top_sellers(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    as_of: date | None = None,
    limit: int = 10,
) -> list[ListingSales]:
    as_of = _today(as_of)
    start, end = _window(as_of, cfg.windows.revenue_window_days)
    rows = conn.execute(
        "SELECT listing_id, SUM(quantity) AS units, SUM(quantity*price_usd) AS revenue_usd "
        "FROM proj_sale_items WHERE user_id=? AND sale_date BETWEEN ? AND ? "
        "GROUP BY listing_id ORDER BY revenue_usd DESC, listing_id LIMIT ?",
        (user_id, start.isoformat(), end.isoformat(), limit),
    ).fetchall()
    return [
        ListingSales(
            listing_id=r["listing_id"],
            title=_title(conn, user_id, r["listing_id"]),
            units=r["units"],
            revenue_usd=round(r["revenue_usd"], 2),
        )
        for r in rows
    ]


def viewed_not_sold(conn: sqlite3.Connection, user_id: int) -> list[ViewedNotSold]:
    # Lifetime, not windowed: "has this listing EVER converted a view into a
    # sale" is the useful question -- proj_listings already carries the
    # last-known (i.e. highest, since views is a monotonic counter) lifetime
    # views value per listing.
    rows = conn.execute(
        "SELECT listing_id, title, views FROM proj_listings "
        "WHERE user_id=? AND views>0 AND listing_id NOT IN ("
        "  SELECT DISTINCT listing_id FROM proj_sale_items WHERE user_id=?"
        ") ORDER BY views DESC, listing_id",
        (user_id, user_id),
    ).fetchall()
    return [
        ViewedNotSold(listing_id=r["listing_id"], title=r["title"], views_lifetime=r["views"])
        for r in rows
    ]


def _dead_listing_candidates(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date
) -> tuple[list[DeadListing], int]:
    """Returns (confirmed-dead listings, count excluded because zero view
    growth could NOT be confirmed -- see _views_delta). Shared by
    dead_listings() and data_quality_notes() so the eligibility/skip logic
    exists in exactly one place; the unmeasurable count is what stops a
    sync gap (or a listing dropping off /listings/active, which is
    active-only) from rendering as a false "0 views" and, at scale, from
    declaring the whole catalog dead the day sync resumes."""
    window_days = cfg.dead_listing.window_days
    min_observed = cfg.dead_listing.min_observed_days
    start, end = _window(as_of, window_days)

    listing_ids = [
        r["listing_id"]
        for r in conn.execute(
            "SELECT DISTINCT listing_id FROM proj_listing_daily WHERE user_id=?", (user_id,)
        ).fetchall()
    ]

    dead: list[DeadListing] = []
    unmeasurable = 0
    for listing_id in listing_ids:
        (first_day_str,) = conn.execute(
            "SELECT MIN(day) FROM proj_listing_daily WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        ).fetchone()
        days_observed = (as_of - date.fromisoformat(first_day_str)).days
        if days_observed < min_observed:
            continue  # not enough history to call this "dead" rather than "new"

        window_rows = conn.execute(
            "SELECT views FROM proj_listing_daily WHERE user_id=? AND listing_id=? "
            "AND day BETWEEN ? AND ? ORDER BY day",
            (user_id, listing_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        views_delta = _views_delta(window_rows)
        if views_delta is None:
            # No, or exactly one, observation in the window: absence of
            # data, not confirmed zero growth. Never guess -- exclude.
            unmeasurable += 1
            continue
        if views_delta > 0:
            continue

        sold_in_window = conn.execute(
            "SELECT 1 FROM proj_sale_items WHERE user_id=? AND listing_id=? "
            "AND sale_date BETWEEN ? AND ? LIMIT 1",
            (user_id, listing_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        if sold_in_window is not None:
            continue

        dead.append(
            DeadListing(
                listing_id=listing_id,
                title=_title(conn, user_id, listing_id),
                days_observed=days_observed,
                views_in_window=views_delta,
            )
        )
    return sorted(dead, key=lambda d: d.listing_id), unmeasurable


def dead_listings(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> list[DeadListing]:
    dead, _unmeasurable = _dead_listing_candidates(conn, user_id, cfg, _today(as_of))
    return dead


def trending(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> list[TrendingListing]:
    as_of = _today(as_of)
    window_days = cfg.windows.trend_window_days
    recent_start, recent_end = _window(as_of, window_days)
    prior_end = recent_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=window_days - 1)

    listing_ids = [
        r["listing_id"]
        for r in conn.execute(
            "SELECT DISTINCT listing_id FROM proj_listing_daily WHERE user_id=?", (user_id,)
        ).fetchall()
    ]

    def _delta_in(listing_id: int, start: date, end: date) -> int | None:
        rows = conn.execute(
            "SELECT views FROM proj_listing_daily WHERE user_id=? AND listing_id=? "
            "AND day BETWEEN ? AND ? ORDER BY day",
            (user_id, listing_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        return _views_delta(rows)

    out: list[TrendingListing] = []
    for listing_id in listing_ids:
        recent_delta = _delta_in(listing_id, recent_start, recent_end)
        prior_delta = _delta_in(listing_id, prior_start, prior_end)
        # Can't claim acceleration without a real reading on both sides.
        if recent_delta is None or prior_delta is None:
            continue
        if recent_delta > 0 and recent_delta > prior_delta:
            out.append(
                TrendingListing(
                    listing_id=listing_id,
                    title=_title(conn, user_id, listing_id),
                    views_recent=recent_delta,
                    views_prior=prior_delta,
                )
            )
    return sorted(out, key=lambda t: (-(t.views_recent - t.views_prior), t.listing_id))


def product_type_breakdown(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> list[ProductTypeStat]:
    as_of = _today(as_of)
    start, end = _window(as_of, cfg.windows.revenue_window_days)
    keywords = cfg.product_type_keywords

    sales_stats: dict[str, dict[str, float]] = {}
    for r in _sale_items_with_titles(conn, user_id, start, end):
        title = r["title"] or f"listing {r['listing_id']}"
        product_type = _classify_product_type(title, keywords)
        bucket = sales_stats.setdefault(product_type, {"units": 0.0, "revenue_usd": 0.0})
        bucket["units"] += r["quantity"]
        bucket["revenue_usd"] += r["quantity"] * r["price_usd"]

    catalog_counts: dict[str, set[int]] = {}
    for r in conn.execute(
        "SELECT listing_id, title FROM proj_listings WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall():
        product_type = _classify_product_type(r["title"], keywords)
        catalog_counts.setdefault(product_type, set()).add(r["listing_id"])

    product_types = set(sales_stats) | set(catalog_counts)
    out = [
        ProductTypeStat(
            product_type=product_type,
            listing_count=len(catalog_counts.get(product_type, set())),
            units=int(sales_stats.get(product_type, {}).get("units", 0)),
            revenue_usd=round(sales_stats.get(product_type, {}).get("revenue_usd", 0.0), 2),
        )
        for product_type in product_types
    ]
    return sorted(out, key=lambda s: (-s.revenue_usd, s.product_type))


def size_breakdown(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> list[SizeStat]:
    as_of = _today(as_of)
    start, end = _window(as_of, cfg.windows.revenue_window_days)

    stats: dict[str | None, dict[str, float]] = {}
    for r in _sale_items_with_titles(conn, user_id, start, end):
        title = r["title"] or f"listing {r['listing_id']}"
        size = _extract_size(title)
        bucket = stats.setdefault(size, {"units": 0.0, "revenue_usd": 0.0})
        bucket["units"] += r["quantity"]
        bucket["revenue_usd"] += r["quantity"] * r["price_usd"]

    out = [
        SizeStat(size=size, units=int(bucket["units"]), revenue_usd=round(bucket["revenue_usd"], 2))
        for size, bucket in stats.items()
    ]
    return sorted(out, key=lambda s: (-s.revenue_usd, s.size or ""))


def shoot_more(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> list[ShootMoreSuggestion]:
    # A proxy signal, not a photographic-subject recommendation: "subject"
    # lives in pipeline's vision-scoring proj_scores, which this module does
    # not read (no model, no cross-module reach beyond ops's own
    # projections + core's). This surfaces PRODUCT TYPES that sell well
    # relative to how few listings carry them -- see the report for why
    # subject-level "shoot more waterfalls" needs M7/vision data this slice
    # doesn't have.
    breakdown = product_type_breakdown(conn, user_id, cfg, as_of=as_of)
    return [
        ShootMoreSuggestion(
            product_type=s.product_type, listing_count=s.listing_count, revenue_usd=s.revenue_usd
        )
        for s in breakdown
        if s.product_type != "unknown"
        and s.revenue_usd > 0
        and s.listing_count <= cfg.shoot_more.max_listing_count
    ]


def data_quality_notes(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> list[str]:
    as_of = _today(as_of)
    notes: list[str] = []

    start, end = _window(as_of, cfg.windows.revenue_window_days)
    rows = _sale_items_with_titles(conn, user_id, start, end)
    if rows:
        no_size = sum(1 for r in rows if _extract_size(r["title"] or "") is None)
        if no_size:
            notes.append(
                f"{no_size} of {len(rows)} sale line items this window have no size PATTERN in "
                "the title at all -- and a match on the rest isn't verified either (a title "
                "listing several sizes, or an aspect-ratio mention like '(2:3)', can match the "
                "wrong number pair). Physical print size lives in Etsy inventory variations, "
                "which sync does not capture per-transaction, so treat this count as a floor on "
                "the gap, not the whole of it."
            )

        unknown = sum(
            1
            for r in rows
            if _classify_product_type(r["title"] or "", cfg.product_type_keywords) == "unknown"
        )
        if unknown:
            notes.append(
                f"{unknown} of {len(rows)} sale line items this window matched no "
                "product_type_keywords entry in ops.json -- the product-type breakdown is "
                "partial."
            )

    # Independent of whether anything sold this window -- a sync gap, or a
    # listing dropping off Etsy's active-only /listings/active, both look
    # like "no observations" and must be reported as that, not folded into
    # dead_listings() as a false "0 views" (see _dead_listing_candidates).
    _, unmeasurable = _dead_listing_candidates(conn, user_id, cfg, as_of)
    if unmeasurable:
        notes.append(
            f"{unmeasurable} listing(s) have no confirmed view reading in the last "
            f"{cfg.dead_listing.window_days}d (no observation, or only one, inside that "
            "window) -- excluded from the dead-listing check rather than assumed dead. Check "
            "that `shopsteward sync` is running; a listing that expired or sold out also stops "
            "appearing, since /listings/active is active-only."
        )

    return notes


def pin_experiment_readout(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: date | None = None
) -> list[PinExperimentResult]:
    """P1 outcome readout (2026-08-24 design doc §3) over proj_pin_experiments
    joined against this listing's own proj_listing_daily views history --
    pure SQL, no LLM, no attribution claim. For each drafted pin, compares
    the listing's views/day in the `cfg.windows.revenue_window_days` days
    immediately before `drafted_at` (baseline) against the same-length
    window immediately after (observed). Reuses revenue_window_days rather
    than adding a new window knob (configuration-over-code precedent).

    **Correlational only** -- a delta could be driven by anything, not
    provably the pin. Both baseline_views_per_day and observed_views_per_day
    are None (never 0) whenever they can't be measured yet:
    - observed is None if fewer than window_days days have elapsed since
      drafted_at (too early -- absence of data, not a zero reading).
    - baseline is None if the listing's observed history doesn't reach back
      window_days days before drafted_at (too new to have a real baseline),
      or if there are fewer than two observations in that window
      (`_views_delta`'s own rule).
    Rendering the "too early"/"no baseline" cases honestly is the caller's
    job (brief.py) -- this function never guesses a number to fill the gap.
    """
    as_of = _today(as_of)
    window_days = cfg.windows.revenue_window_days
    # Most-recent-_PIN_EXPERIMENTS_MAX_ROWS first (see module-level comment
    # on the constant), then reversed back to the original oldest-first
    # display order.
    rows = list(
        reversed(
            conn.execute(
                "SELECT listing_id, action_id, drafted_at FROM proj_pin_experiments "
                "WHERE user_id=? ORDER BY drafted_at DESC, action_id DESC LIMIT ?",
                (user_id, _PIN_EXPERIMENTS_MAX_ROWS),
            ).fetchall()
        )
    )

    out: list[PinExperimentResult] = []
    for r in rows:
        listing_id = r["listing_id"]
        drafted_date = date.fromisoformat(r["drafted_at"][:10])

        before_end = drafted_date - timedelta(days=1)
        before_start = before_end - timedelta(days=window_days - 1)
        after_start = drafted_date + timedelta(days=1)
        after_end = after_start + timedelta(days=window_days - 1)

        baseline: float | None = None
        (min_day_str,) = conn.execute(
            "SELECT MIN(day) FROM proj_listing_daily WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        ).fetchone()
        if min_day_str is not None and date.fromisoformat(min_day_str) <= before_start:
            before_rows = conn.execute(
                "SELECT day, views FROM proj_listing_daily WHERE user_id=? AND listing_id=? "
                "AND day BETWEEN ? AND ? ORDER BY day",
                (user_id, listing_id, before_start.isoformat(), before_end.isoformat()),
            ).fetchall()
            baseline = _views_rate_per_day(before_rows, window_days)

        observed: float | None = None
        if as_of >= after_end:
            after_rows = conn.execute(
                "SELECT day, views FROM proj_listing_daily WHERE user_id=? AND listing_id=? "
                "AND day BETWEEN ? AND ? ORDER BY day",
                (user_id, listing_id, after_start.isoformat(), after_end.isoformat()),
            ).fetchall()
            observed = _views_rate_per_day(after_rows, window_days)

        delta_per_day = None if baseline is None or observed is None else observed - baseline

        out.append(
            PinExperimentResult(
                listing_id=listing_id,
                action_id=r["action_id"],
                title=_title(conn, user_id, listing_id),
                drafted_at=drafted_date.isoformat(),
                days_since_posted=(as_of - drafted_date).days,
                baseline_views_per_day=baseline,
                observed_views_per_day=observed,
                delta_views_per_day=delta_per_day,
            )
        )
    return out
