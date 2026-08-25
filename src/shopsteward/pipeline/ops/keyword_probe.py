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

import logging
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import median

import pydantic
from pydantic import BaseModel, Field

from shopsteward.adapters.etsy.interface import EtsyAdapter
from shopsteward.adapters.etsy.models import EtsyActiveListingResult
from shopsteward.core.events import Event, append
from shopsteward.core.sync import read_live_observed
from shopsteward.pipeline.ops.models import OpsConfig
from shopsteward.pipeline.ops.timeutil import parse_ts

_logger = logging.getLogger(__name__)


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


# --- phrase -> listing bridge (listing.seo_edit wiring, 2026-08-25) ---------
#
# Probes are stored keyed by PHRASE (module docstring above); a capability
# needs them keyed by LISTING. The bridge below is entirely deterministic --
# never the LLM's job to decide "does this probe apply to my listing".

# A tiny, deliberately generic stopword list -- just enough that a phrase
# consisting ONLY of stopwords (never happens for a real probed phrase, but
# defensive) can never match by construction, and that a single shared
# stopword (e.g. both title and phrase contain "and") is never counted as
# the "meaningful overlap" the matching rule below requires.
_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS}


def _phrase_matches_title(phrase_tokens: set[str], title_tokens: set[str]) -> bool:
    """The matching rule (module docstring for `ListingKeywordSignal` below
    states this too): a probed phrase matches a listing iff EVERY
    non-stopword token in the phrase is also present among the listing
    title's non-stopword tokens (case/punctuation normalized, order-
    independent). A phrase that reduces to zero non-stopword tokens can
    never match (guards the always-false edge case, not a real probe
    input). This is a strict full-phrase-subset match, not "any shared
    word" -- one shared word (a single-stopword-strength match) is
    explicitly NOT enough: "elk wall art" must NOT match a "Bison Wall Art
    Print" title just because both share "wall"/"art"; every token in the
    phrase must be present."""
    return bool(phrase_tokens) and phrase_tokens.issubset(title_tokens)


def _is_safe_ranker_tag(tag: str, cfg: OpsConfig) -> bool:
    """Two cheap, config-driven filters over a tag before it is ever
    surfaced as "ranker-rewarded" (module docstring's honesty guardrails):

    1. `brand_denylist_substrings` -- trademark symbols by default. This is
       a floor, not a full brand-name detector (Etsy tags are free-text
       and this shop has no reliable way to distinguish a generic category
       phrase like "cabin wall decor" from a genuine competitor shop name
       algorithmically) -- catches the cheap, unambiguous case.
    2. `non_photo_medium_terms` -- the shop's real differentiation is
       PHOTOGRAPHY (docs/research/2026-08-24-etsy-path-to-profitability.md
       "Positioning", policy E16): several probe results tag competing
       LISTINGS "Yellowstone Painting"/"landscape painting" even though
       this shop's own catalog is entirely photographs. Adopting such a
       tag onto a photograph would misrepresent it -- filtered here,
       before the LLM ever sees it as a candidate, not just told not to
       use it (belt-and-suspenders with the prompt instruction)."""
    lowered = tag.lower()
    # L1 (guardrail review 2026-08-25): both lists compared against `lowered`
    # -- comparing brand_denylist_substrings against the RAW tag meant the
    # first case-varying brand entry an operator adds would silently miss.
    # Symbols (™/®/©/℠) are unaffected by casing either way.
    if any(bad.lower() in lowered for bad in cfg.keyword_probe.brand_denylist_substrings):
        return False
    return not any(term in lowered for term in cfg.keyword_probe.non_photo_medium_terms)


class ListingKeywordSignal(BaseModel):
    """Per-listing bridge from `etsy.keyword.probed` (phrase-keyed) to a
    concrete listing_id, computed by `listing_keyword_signal()` below --
    never persisted as its own event (it is a deterministic re-derivation
    from existing `etsy.keyword.probed` events + the listing's current
    title, always reproducible from the log, so writing it separately would
    just be a cache with its own staleness problem).

    `ranker_tags` is the union of `tag_frequency` from every FRESH
    (`age_days <= keyword_probe.max_age_days`), title-MATCHING
    (`_phrase_matches_title`) probe for this listing, summed and ranked by
    total frequency (desc), tag name (asc) as a deterministic tie-break --
    already passed through `_is_safe_ranker_tag`, so every tag here is safe
    to hand to the LLM as a candidate. `probed_at`/`age_days` describe the
    FRESHEST matching probe used (the most favorable honest reading, since
    a listing may match more than one phrase at different ages)."""

    listing_id: int
    matched_phrases: list[str]
    ranker_tags: list[str]
    probed_at: str  # ISO timestamp of the freshest matching probe
    age_days: int


def listing_keyword_signal(
    conn: sqlite3.Connection,
    user_id: int,
    cfg: OpsConfig,
    listing_id: int,
    title: str,
    *,
    as_of: datetime | None = None,
) -> ListingKeywordSignal | None:
    """Returns `None` -- never an empty/misleading signal -- when this
    listing has no fresh, title-matching probe (module docstring's
    absence-is-not-zero rule, reused for keyword-probe coverage too):
    covers both "never probed" and "probed, but the freshest reading for
    every matching phrase is older than `keyword_probe.max_age_days`" and
    "matched a phrase, but every rewarded tag was filtered by
    `_is_safe_ranker_tag`" identically -- callers must never render an
    empty `ranker_tags` list as "no tags are rewarded", since that is a
    different (and false) claim than "no fresh signal exists".

    Uses only the LATEST `etsy.keyword.probed` event per phrase (events
    accumulate append-only as a phrase is re-probed over time -- module
    docstring; a stale reading for a phrase that was since re-probed fresh
    must never win just because it happened to still be within
    `max_age_days`)."""
    as_of = as_of or datetime.now(UTC)
    max_age = timedelta(days=cfg.keyword_probe.max_age_days)

    latest_by_phrase: dict[str, tuple[datetime, KeywordProbeResult]] = {}
    # read_live_observed (H1, guardrail review 2026-08-25): the SAME
    # fixture-pollution guard `seo_edit._latest_observed` uses for
    # `etsy.listing.observed`, reused here as-is. Its anchor is a global
    # event-id boundary (the earliest `etsy.shop.observed` for the
    # currently-configured shop), not anything keyed by listing_id -- so it
    # applies identically to phrase-keyed `etsy.keyword.probed` rows: a
    # `probe-keyword --fixtures` smoke-test run before the real shop's first
    # `--live` sync is excluded exactly like a fixture-era listing/sale would
    # be, with no new "source" field needed.
    for e in read_live_observed(conn, "etsy.keyword.probed"):
        if e.user_id != user_id or e.created_at is None:
            continue
        try:
            result = KeywordProbeResult.model_validate(e.payload)
            phrase = e.payload["phrase"]
        except (pydantic.ValidationError, KeyError):
            # H2 (guardrail review 2026-08-25): one malformed/schema-drifted
            # row must never take down every planner run -- skip it, same
            # pattern as seo_edit._latest_observed's own model_validate
            # guard, and note it so the skip is observable, not silent.
            _logger.warning("skipping malformed etsy.keyword.probed event id=%s", e.id)
            continue
        created_at = parse_ts(e.created_at)
        prior = latest_by_phrase.get(phrase)
        if prior is None or created_at > prior[0]:
            latest_by_phrase[phrase] = (created_at, result)

    title_tokens = _content_tokens(title)
    matched_phrases: list[str] = []
    tag_counts: Counter[str] = Counter()
    freshest: datetime | None = None
    for phrase, (created_at, result) in latest_by_phrase.items():
        if as_of - created_at > max_age:
            continue  # stale -- excluded, never served (module docstring)
        if not _phrase_matches_title(_content_tokens(phrase), title_tokens):
            continue
        matched_phrases.append(phrase)
        tag_counts.update(result.aggregates.tag_frequency)
        if freshest is None or created_at > freshest:
            freshest = created_at

    if not matched_phrases or freshest is None:
        return None

    ranker_tags = [
        tag
        for tag, _n in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if _is_safe_ranker_tag(tag, cfg)
    ]
    if not ranker_tags:
        return None  # everything rewarded was filtered out -- nothing honest left to surface

    return ListingKeywordSignal(
        listing_id=listing_id,
        matched_phrases=sorted(matched_phrases),
        ranker_tags=ranker_tags,
        probed_at=freshest.isoformat(),
        age_days=(as_of - freshest).days,
    )


def probe_coverage_note(
    conn: sqlite3.Connection, user_id: int, cfg: OpsConfig, as_of: datetime | None = None
) -> str | None:
    """Operator visibility (listing.seo_edit wiring item 5): among ACTIVE
    listings, how many currently have a fresh, title-matching probe signal
    -- so "why is this listing's seo_edit un-informed" is answerable from
    the brief without re-deriving keyword-probe matching by hand. Scoped to
    ALL active listings, not just today's seo_edit-eligible subset --
    eligibility itself shifts run to run (views/cooldown/sale-window
    change), while probe coverage does not; this is the simpler, more
    stable question, and a listing that becomes seo_edit-eligible next week
    still benefits from knowing its coverage today.

    Returns `None` (nothing to report, never a misleading "0 of 0") when no
    probe has ever been run for this user, or there are no active listings
    -- same absence-is-not-zero convention as everything else in this
    module."""
    as_of = as_of or datetime.now(UTC)
    if not any(e.user_id == user_id for e in read_live_observed(conn, "etsy.keyword.probed")):
        return None

    rows = conn.execute(
        "SELECT listing_id, title FROM proj_listings WHERE user_id=? AND state='active'",
        (user_id,),
    ).fetchall()
    if not rows:
        return None

    # ponytail: O(active listings x probe events) -- one full
    # `listing_keyword_signal()` re-read of every etsy.keyword.probed event
    # per active listing. Fine at this shop's scale (~27 listings, a
    # handful of probes/week); if either grows enough to matter, hoist a
    # single shared `latest_by_phrase` pass out of the per-listing loop.
    covered = 0
    uncovered_titles: list[str] = []
    for r in rows:
        signal = listing_keyword_signal(
            conn, user_id, cfg, r["listing_id"], r["title"], as_of=as_of
        )
        if signal is None:
            uncovered_titles.append(r["title"])
        else:
            covered += 1

    note = (
        f"{covered} of {len(rows)} active listing(s) have fresh keyword-probe tag coverage "
        f"matching their title (max age {cfg.keyword_probe.max_age_days}d)."
    )
    if uncovered_titles:
        sample = ", ".join(uncovered_titles[:3])
        more = "..." if len(uncovered_titles) > 3 else ""
        note += f" No coverage: {sample}{more}"
    return note
