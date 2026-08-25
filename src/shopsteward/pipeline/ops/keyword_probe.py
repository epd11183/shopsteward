"""Etsy keyword/competition probe -- the free first-party demand-signal
source this shop has never used (verified 2026-08-25 against the live Etsy
OAS): Etsy publishes no keyword-volume API and never has; every paid tool
(eRank/Marmalade/EverBee/Alura) sells a proprietary scrape, not a public
API. `findAllListingsActive` (adapters.etsy `find_active_listings`,
x-api-key only, no new scope) yields most of what those tools sell:
`count` (total matching listings) is a competition proxy; the top-N results
ranked by Etsy's own relevance sorter (`sort_on="score"`) reveal the tag set
Etsy's ranker actually rewards, their price range, and a favorites-per-day
demand proxy.

READ-ONLY MARKET RESEARCH -- deliberately NOT wrapped as an autonomy-chassis
capability (no Tier, no ProposedAction, no governor). Nothing here spends
money or writes to Etsy; there is nothing for PROPOSE/NOTIFY/AUTO to gate.

WHAT IS PERSISTED, AND WHY: one `etsy.keyword.probed` event per probe,
carrying the phrase, the filters (taxonomy_id/min_price/max_price), the
requested top_n, `count` (competition), and the DERIVED aggregates below.
Events accumulate append-only into a time series, so probing the same
phrase again next month lets `analytics`-style code compare readings over
time, exactly like `etsy.listing.observed` already does for this shop's own
catalog.

WHAT IS NEVER PERSISTED: the raw competitor listing rows `find_active_listings`
returns (title, exact price, exact favorite count, url, ...) are used
in-memory to compute the aggregates below and then DISCARDED -- never
written to the event log. This repo is PUBLIC; accumulating scraped
third-party catalogue rows over time would be exactly the kind of dataset
that must never land in a public git history. Only bounded, derived
aggregates (counts, frequencies, medians/min/max) are stored.

Our own shop's listings are excluded from the sample before any aggregate
is computed (matched by `listing_id` against `proj_listings`, this user's
already-synced catalog) -- otherwise a probe of our own best-selling phrase
would "learn" our own tags/price back as if they were competitive signal.
"""

import sqlite3
from collections import Counter
from datetime import UTC, datetime
from statistics import median

from pydantic import BaseModel, Field

from shopsteward.adapters.etsy.interface import EtsyAdapter
from shopsteward.adapters.etsy.models import EtsyActiveListingResult
from shopsteward.core.events import Event, append
from shopsteward.pipeline.ops.models import OpsConfig


class KeywordProbeAggregates(BaseModel):
    """Derived-only signal computed over the (own-listings-excluded) sample
    of up to `top_n` results ranked by Etsy's own relevance sorter.
    `sample_size` can be smaller than the requested `top_n` when some of the
    top-ranked results turned out to be our own listings (excluded) --
    Etsy's search API has no "exclude this shop" filter to ask for a bigger
    page instead.

    Every numeric field is `None`, never 0, when `sample_size == 0` --
    "absent is not zero" (this chassis's pin analytics precedent,
    `analytics._views_delta`): a probe that matched nothing tells you
    nothing about price or demand, and rendering that as $0/0 favorites
    would be a lie, not a finding."""

    sample_size: int
    tag_frequency: dict[str, int] = Field(default_factory=dict)
    median_price_usd: float | None
    min_price_usd: float | None
    max_price_usd: float | None
    median_favorites_per_day: float | None
    min_favorites_per_day: float | None
    max_favorites_per_day: float | None


class KeywordProbeResult(BaseModel):
    """One probe's full result -- this is also exactly what gets persisted
    to `etsy.keyword.probed`'s payload (see module docstring for what is
    and is not stored)."""

    phrase: str
    taxonomy_id: int | None = None
    min_price: float | None = None
    max_price: float | None = None
    top_n: int
    competition_count: int
    aggregates: KeywordProbeAggregates


def _compute_aggregates(
    sample: list[EtsyActiveListingResult], as_of: datetime
) -> KeywordProbeAggregates:
    if not sample:
        return KeywordProbeAggregates(
            sample_size=0,
            tag_frequency={},
            median_price_usd=None,
            min_price_usd=None,
            max_price_usd=None,
            median_favorites_per_day=None,
            min_favorites_per_day=None,
            max_favorites_per_day=None,
        )

    tag_counts: Counter[str] = Counter()
    prices: list[float] = []
    favorites_per_day: list[float] = []
    for row in sample:
        tag_counts.update(row.tags)
        prices.append(row.price.as_float)
        # creation_timestamp is EPOCH SECONDS (Etsy's real response shape) --
        # explicit conversion, never a string-slice comparison (timeutil's
        # own rule, applied here even though this isn't a stored ISO string).
        created_at = datetime.fromtimestamp(row.creation_timestamp, tz=UTC)
        age_days = max(1, (as_of - created_at).days)  # avoid a same-day divide-by-zero
        favorites_per_day.append(row.num_favorers / age_days)

    return KeywordProbeAggregates(
        sample_size=len(sample),
        tag_frequency=dict(tag_counts),
        median_price_usd=round(median(prices), 2),
        min_price_usd=round(min(prices), 2),
        max_price_usd=round(max(prices), 2),
        median_favorites_per_day=round(median(favorites_per_day), 3),
        min_favorites_per_day=round(min(favorites_per_day), 3),
        max_favorites_per_day=round(max(favorites_per_day), 3),
    )


def probe_keyword(
    conn: sqlite3.Connection,
    user_id: int,
    adapter: EtsyAdapter,
    cfg: OpsConfig,
    phrase: str,
    *,
    taxonomy_id: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    as_of: datetime | None = None,
) -> KeywordProbeResult:
    """Probe one phrase, compute aggregates, and append `etsy.keyword.probed`.
    `as_of` is injected (defaults to wall-clock now) so a probe's
    favorites-per-day computation is deterministic and testable -- same
    convention as every `as_of`-taking read in analytics.py, even though
    this function, unlike those, also writes an event: the write itself
    just records whatever `as_of` (or its default) computed, it never
    re-derives "now" a second time."""
    as_of = as_of or datetime.now(UTC)
    top_n = cfg.keyword_probe.top_n

    page = adapter.find_active_listings(
        phrase,
        taxonomy_id=taxonomy_id,
        min_price=min_price,
        max_price=max_price,
        limit=top_n,
        sort_on="score",
    )

    own_listing_ids = {
        row["listing_id"]
        for row in conn.execute(
            "SELECT listing_id FROM proj_listings WHERE user_id=?", (user_id,)
        ).fetchall()
    }
    sample = [row for row in page.results if row.listing_id not in own_listing_ids]

    result = KeywordProbeResult(
        phrase=phrase,
        taxonomy_id=taxonomy_id,
        min_price=min_price,
        max_price=max_price,
        top_n=top_n,
        competition_count=page.count,
        aggregates=_compute_aggregates(sample, as_of),
    )

    append(
        conn,
        Event(user_id=user_id, type="etsy.keyword.probed", payload=result.model_dump()),
    )
    return result
