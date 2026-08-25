"""`keyword_probe.listing_keyword_signal` -- the deterministic phrase ->
listing bridge that feeds `listing.seo_edit` ranker-rewarded-tag facts:
positive/negative/near-miss matching, stale-probe exclusion (absence, never
an empty block), freshness re-derivation from the LATEST reading per
phrase, the brand/trademark and non-photo-medium honesty filters, and
determinism for a fixed `as_of`. Also covers `probe_coverage_note`'s own
absence-is-not-zero rule. Entirely on fakes/fixture events -- zero network."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops.config import load_ops_config
from shopsteward.pipeline.ops.keyword_probe import (
    KeywordProbeAggregates,
    KeywordProbeResult,
    _is_safe_ranker_tag,
    listing_keyword_signal,
    probe_coverage_note,
)
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
AS_OF = datetime(2026, 8, 25, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


@pytest.fixture()
def cfg():
    return load_ops_config()


def _seed_probe(
    conn,
    *,
    phrase: str,
    tag_frequency: dict[str, int],
    created_at: datetime,
    user_id: int = USER_ID,
) -> None:
    result = KeywordProbeResult(
        phrase=phrase,
        top_n=25,
        competition_count=100,
        aggregates=KeywordProbeAggregates(
            sample_size=sum(tag_frequency.values()) or 1,
            tag_frequency=tag_frequency,
            median_price_usd=30.0,
            min_price_usd=20.0,
            max_price_usd=40.0,
            median_favorites_per_day=1.0,
            min_favorites_per_day=0.5,
            max_favorites_per_day=2.0,
        ),
    )
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (
            user_id,
            "etsy.keyword.probed",
            json.dumps(result.model_dump()),
            created_at.isoformat().replace("+00:00", "Z"),
        ),
    )
    conn.commit()


# --- matching rule -----------------------------------------------------------


def test_positive_match_yields_ranked_tags(conn, cfg):
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5, "buffalo print": 3, "cabin wall decor": 3},
        created_at=AS_OF - timedelta(days=1),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )

    assert signal is not None
    assert signal.matched_phrases == ["bison wall art"]
    # ranked by frequency desc, tag name asc tie-break
    assert signal.ranker_tags == ["western wall art", "buffalo print", "cabin wall decor"]
    assert signal.age_days == 1


def test_negative_no_probe_at_all_returns_none(conn, cfg):
    assert (
        listing_keyword_signal(conn, USER_ID, cfg, 501, "Bison Wall Art Print", as_of=AS_OF) is None
    )


def test_negative_probe_exists_but_phrase_does_not_appear_in_title(conn, cfg):
    _seed_probe(
        conn,
        phrase="great blue heron",
        tag_frequency={"bird wall art": 4},
        created_at=AS_OF - timedelta(days=1),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is None


def test_KNOWN_LIMITATION_order_independent_matching_matches_wrong_vocabulary(conn, cfg):
    # M3 (guardrail review 2026-08-25): `_phrase_matches_title` is a set
    # containment check, so it is ORDER-INDEPENDENT -- "black bear" matches
    # a title that merely contains "black" and "bear" as separate words,
    # even split across unrelated phrases ("Bear Photograph in Black and
    # White"). Documented, not fixed: the cost is a wrong-vocabulary tag
    # suggestion, never a wrong TARGET (the listing itself is still real and
    # grounded) -- do not redesign the matcher for this.
    _seed_probe(
        conn,
        phrase="black bear",
        tag_frequency={"wildlife photography": 4},
        created_at=AS_OF - timedelta(days=1),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bear Photograph in Black and White", as_of=AS_OF
    )
    assert signal is not None
    assert signal.matched_phrases == ["black bear"]


def test_near_miss_partial_token_overlap_does_not_match(conn, cfg):
    # "elk wall art" shares "wall"/"art" with the title but NOT "elk" --
    # a single shared stopword-strength overlap must never count as a match.
    _seed_probe(
        conn,
        phrase="elk wall art",
        tag_frequency={"woodland decor": 2},
        created_at=AS_OF - timedelta(days=1),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is None


# --- freshness ---------------------------------------------------------------


def test_stale_probe_is_excluded_and_yields_no_block_not_an_empty_one(conn, cfg):
    # default max_age_days=90
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5},
        created_at=AS_OF - timedelta(days=91),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is None  # absence, never an empty ranker_tags list


def test_fresh_probe_at_exactly_the_boundary_is_kept(conn, cfg):
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5},
        created_at=AS_OF - timedelta(days=90),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is not None
    assert signal.age_days == 90


def test_re_probing_a_phrase_uses_only_the_latest_reading(conn, cfg):
    # An old, stale reading for "bison wall art" would be excluded on its
    # own, but the phrase was re-probed fresh since -- the latest reading
    # wins, not the oldest.
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"stale tag": 9},
        created_at=AS_OF - timedelta(days=200),
    )
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"fresh tag": 5},
        created_at=AS_OF - timedelta(days=2),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is not None
    assert signal.ranker_tags == ["fresh tag"]
    assert signal.age_days == 2


# --- honesty guardrails -------------------------------------------------------


def test_misrepresentation_guard_never_surfaces_a_painting_tag_for_a_photograph(conn, cfg):
    _seed_probe(
        conn,
        phrase="yellowstone wall art",
        tag_frequency={"yellowstone painting": 6, "national park wall art": 4},
        created_at=AS_OF - timedelta(days=1),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Yellowstone Wall Art Photograph", as_of=AS_OF
    )
    assert signal is not None
    assert "yellowstone painting" not in signal.ranker_tags
    assert signal.ranker_tags == ["national park wall art"]


def test_brand_denylist_filters_trademark_tags(conn, cfg):
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5, "acme prints™": 9},
        created_at=AS_OF - timedelta(days=1),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph", as_of=AS_OF
    )
    assert signal is not None
    assert "acme prints™" not in signal.ranker_tags
    assert signal.ranker_tags == ["western wall art"]


def test_all_rewarded_tags_filtered_out_yields_none_not_empty_list(conn, cfg):
    _seed_probe(
        conn,
        phrase="yellowstone wall art",
        tag_frequency={"yellowstone painting": 6},
        created_at=AS_OF - timedelta(days=1),
    )
    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Yellowstone Wall Art Photograph", as_of=AS_OF
    )
    assert signal is None


# --- M1: medium-vocabulary denylist under/over-block (guardrail review 2026-08-25) --


@pytest.mark.parametrize(
    "tag",
    [
        "watercolor wall art",
        "watercolour print",
        "acrylic painting",
        "oil paint landscape",
        "hand painted print",
        "painted portrait",
        "charcoal drawing",
        "pastel artwork",
        "cartoon style print",
        "anime fan art",
        "ai art print",
        "ai generated wall art",
        "vector illustration",
        "hand drawn sketch",  # "drawn" -- "drawing" alone would have missed this
    ],
)
def test_under_block_regression_catches_previously_missed_media(tag, cfg):
    assert _is_safe_ranker_tag(tag, cfg) is False


@pytest.mark.parametrize(
    "tag",
    [
        "wall art",
        "fine art print",
        "art print",
        "nature photography",
        "national park wall art",
        # A real print SUBSTRATE this shop can sell -- product_type_keywords
        # has an `acrylic` type, so blocking the bare word would drop a
        # legitimate product tag, not a misrepresentation.
        "acrylic print",
        # COLOUR words, not media. Blocking these would gut ordinary
        # landscape/decor vocabulary.
        "charcoal gray wall art",
        "pastel sunset photograph",
    ],
)
def test_over_block_regression_still_passes_real_photography_tags(tag, cfg):
    assert _is_safe_ranker_tag(tag, cfg) is True


@pytest.mark.parametrize(
    "tag",
    ["acrylic painting", "charcoal drawing", "pastel drawing", "hand painted landscape"],
)
def test_medium_senses_of_substrate_and_colour_words_are_still_blocked(tag, cfg):
    """The bare words `acrylic`/`charcoal`/`pastel` are deliberately NOT in the
    denylist (see the _OpsKeywordProbe default's comment) -- their misrepresenting
    senses are caught by the `paint`/`draw` stems instead. This pins that, so a
    future edit cannot quietly drop the stems and leave the media unblocked."""
    assert _is_safe_ranker_tag(tag, cfg) is False


def test_brand_denylist_is_case_insensitive(cfg):
    # L1 (guardrail review 2026-08-25): brand list now compares against the
    # LOWERED tag, same as the medium list already did -- a case-varying
    # brand entry no longer silently misses.
    cfg.keyword_probe.brand_denylist_substrings = ["Acme Prints"]
    assert _is_safe_ranker_tag("acme prints wall art", cfg) is False


# --- determinism ---------------------------------------------------------------


def test_deterministic_for_a_fixed_as_of(conn, cfg):
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5, "buffalo print": 5},
        created_at=AS_OF - timedelta(days=1),
    )
    first = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph", as_of=AS_OF
    )
    second = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph", as_of=AS_OF
    )
    assert first == second


# --- probe_coverage_note -------------------------------------------------------


def test_probe_coverage_note_absent_when_nothing_ever_probed(conn, cfg):
    seed_listing_observed_on(
        conn, listing_id=501, title="Bison Wall Art Photograph", day=AS_OF.date(), views=5
    )
    rebuild_core(conn)
    assert probe_coverage_note(conn, USER_ID, cfg, as_of=AS_OF) is None


def test_probe_coverage_note_counts_covered_and_uncovered(conn, cfg):
    seed_listing_observed_on(
        conn, listing_id=501, title="Bison Wall Art Photograph", day=AS_OF.date(), views=5
    )
    seed_listing_observed_on(
        conn, listing_id=502, title="Uncovered Loon Print", day=AS_OF.date(), views=5
    )
    rebuild_core(conn)
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5},
        created_at=AS_OF - timedelta(days=1),
    )
    note = probe_coverage_note(conn, USER_ID, cfg, as_of=AS_OF)
    assert note is not None
    assert "1 of 2 active listing(s)" in note
    assert "Uncovered Loon Print" in note


# --- H1: fixture-pollution guard (guardrail review 2026-08-25) ---------------


def test_fixture_sourced_probe_before_the_real_shop_anchor_never_reaches_the_signal(
    conn, cfg, tmp_path, monkeypatch
):
    """A `probe-keyword --fixtures` smoke-test run BEFORE the real shop's
    first `--live` sync must never surface its synthetic tags as if Etsy's
    ranker actually rewards them -- mirrors read_live_observed()'s own guard
    test in tests/core/test_sync.py and seo_edit.py's
    test_expired_listing_with_only_fixture_polluted_sales_is_not_eligible."""
    import shopsteward.adapters.etsy.auth as auth_mod
    from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
    from shopsteward.core.events import Event, append

    monkeypatch.setattr(auth_mod, "etsy_tokens_path", lambda: tmp_path / "etsy_tokens.json")

    # fixture-era shop -- predates the real shop.observed anchor.
    append(conn, Event(user_id=USER_ID, type="etsy.shop.observed", payload={"shop_id": 100001}))
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"fixture only tag": 9},
        created_at=AS_OF - timedelta(days=1),
    )
    # real shop anchor, after the fixture probe -- no real probe follows it.
    append(conn, Event(user_id=USER_ID, type="etsy.shop.observed", payload={"shop_id": 52644245}))

    store = EtsyTokenStore()
    store.save(
        EtsyTokens(
            access_token="t",
            access_expires_at=9999999999.0,
            refresh_token="r",
            shop_id=52644245,
            etsy_user_id=1,
            scopes=["shops_r"],
        )
    )

    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is None  # the fixture-era probe never counted


def test_live_sourced_probe_after_the_real_shop_anchor_does_reach_the_signal(
    conn, cfg, tmp_path, monkeypatch
):
    """The complementary direction: a probe appended AFTER the real shop's
    anchor is trusted, exactly as before this guard was added."""
    import shopsteward.adapters.etsy.auth as auth_mod
    from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
    from shopsteward.core.events import Event, append

    monkeypatch.setattr(auth_mod, "etsy_tokens_path", lambda: tmp_path / "etsy_tokens.json")

    append(conn, Event(user_id=USER_ID, type="etsy.shop.observed", payload={"shop_id": 52644245}))
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5},
        created_at=AS_OF - timedelta(days=1),
    )

    store = EtsyTokenStore()
    store.save(
        EtsyTokens(
            access_token="t",
            access_expires_at=9999999999.0,
            refresh_token="r",
            shop_id=52644245,
            etsy_user_id=1,
            scopes=["shops_r"],
        )
    )

    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is not None
    assert signal.ranker_tags == ["western wall art"]


# --- H2: malformed-row resilience (guardrail review 2026-08-25) -------------


def test_one_malformed_probe_row_never_blocks_a_good_one(conn, cfg):
    """A legacy/hand-inserted/schema-drifted `etsy.keyword.probed` row must
    be skipped, not raise and take down every planner run -- mirrors
    seo_edit._latest_observed's own model_validate guard."""
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (
            USER_ID,
            "etsy.keyword.probed",
            json.dumps({"not_a_phrase_field": True}),  # missing required "phrase"/"aggregates"
            (AS_OF - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        ),
    )
    conn.commit()
    _seed_probe(
        conn,
        phrase="bison wall art",
        tag_frequency={"western wall art": 5},
        created_at=AS_OF - timedelta(days=1),
    )

    signal = listing_keyword_signal(
        conn, USER_ID, cfg, 501, "Bison Wall Art Photograph 16x20", as_of=AS_OF
    )
    assert signal is not None
    assert signal.ranker_tags == ["western wall art"]
