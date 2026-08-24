"""`social.pinterest_post` Variant A (`social.pin_drafted`, 2026-08-24 design
doc §2) -- Claude drafts a Pinterest pin for an active, imaged,
not-recently-pinned listing; the operator pastes it into Pinterest by hand.
**No Pinterest call, anywhere** -- `adapters/pinterest` is never imported."""

from datetime import UTC, datetime, timedelta

import pytest

from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.pinterest_post import SocialPinterestPost
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()

LISTING_NEVER_PINNED = 701  # active, has an image, never pinned
LISTING_RECENTLY_PINNED = 702  # active, has an image, pinned 3 days ago
LISTING_NO_IMAGE = 703  # active, no observed image
LISTING_INACTIVE = 704  # inactive, has an image


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_image(conn, listing_id, url="https://example.com/img-570.jpg"):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.images.observed",
            payload={
                "listing_id": listing_id,
                "images": [{"listing_image_id": 1, "rank": 1, "url_570xN": url}],
            },
        ),
    )


def _seed_pin_event(conn, listing_id, event_type, days_ago):
    # Matches core/db.py's schema-default format (%Y-%m-%dT%H:%M:%fZ), not
    # datetime.isoformat()'s "+00:00" suffix -- lexical cutoff comparisons
    # in pinterest_post.py must be exercised against the real format.
    created_at = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (
            USER_ID,
            event_type,
            (
                f'{{"listing_id": {listing_id}, "title": "x", "description": "x", '
                '"alt_text": "x", "board_key": "wall_art", "destination_url": "x", '
                '"image_url": "x"}'
            ),
            created_at,
        ),
    )
    conn.commit()


def _seed_scenario(conn):
    seed_listing_observed_on(
        conn, listing_id=LISTING_NEVER_PINNED, title="Never Pinned Print", day=TODAY, views=10
    )
    _seed_image(conn, LISTING_NEVER_PINNED)

    seed_listing_observed_on(
        conn, listing_id=LISTING_RECENTLY_PINNED, title="Recently Pinned Print", day=TODAY, views=10
    )
    _seed_image(conn, LISTING_RECENTLY_PINNED)
    _seed_pin_event(conn, LISTING_RECENTLY_PINNED, "social.pin_drafted", days_ago=3)

    seed_listing_observed_on(
        conn, listing_id=LISTING_NO_IMAGE, title="No Image Print", day=TODAY, views=10
    )
    # deliberately no image event for LISTING_NO_IMAGE

    seed_listing_observed_on(
        conn,
        listing_id=LISTING_INACTIVE,
        title="Inactive Print",
        day=TODAY,
        views=10,
        state="inactive",
    )
    _seed_image(conn, LISTING_INACTIVE)

    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)


def _cfg():
    return ops_config.load_ops_config()


def _intent(target_id: str, **params) -> ProposalIntent:
    return ProposalIntent(
        capability_key="social.pinterest_post",
        target_id=target_id,
        params=params,
        reason="the LLM's own sentence -- must never become the audit reason",
    )


def _valid_params(**overrides):
    params = {
        "title": "Sandhill Cranes at Dawn",
        "description": "Fine art print for the nature lover's wall.",
        "alt_text": "A print of sandhill cranes at dawn",
        "board_key": "wall_art",
    }
    params.update(overrides)
    return params


# --- registration / no external call -----------------------------------------


def test_module_never_imports_pinterest_or_any_external_adapter():
    import shopsteward.pipeline.ops.capabilities.pinterest_post as mod

    with open(mod.__file__, encoding="utf-8") as f:
        src = f.read()
    for banned in ("adapters.pinterest", "adapters.meta", "adapters.etsy", "httpx", "requests"):
        assert banned not in src


def test_registers_at_propose_tier():
    cap = SocialPinterestPost()
    register(cap)
    assert REGISTRY["social.pinterest_post"].max_tier == Tier.PROPOSE
    assert cap.undo is None


def test_propose_always_returns_empty(conn):
    _seed_scenario(conn)
    cap = SocialPinterestPost()
    assert cap.propose(conn, USER_ID, _cfg()) == []


# --- _candidates() eligibility -----------------------------------------------


def test_candidates_returns_never_pinned_active_imaged_listing(conn):
    _seed_scenario(conn)
    from shopsteward.pipeline.ops.capabilities.pinterest_post import _candidates

    targets = _candidates(conn, USER_ID, _cfg())
    assert str(LISTING_NEVER_PINNED) in targets


def test_candidates_excludes_within_cooldown(conn):
    _seed_scenario(conn)
    from shopsteward.pipeline.ops.capabilities.pinterest_post import _candidates

    targets = _candidates(conn, USER_ID, _cfg())
    assert str(LISTING_RECENTLY_PINNED) not in targets


def test_candidates_excludes_no_image(conn):
    _seed_scenario(conn)
    from shopsteward.pipeline.ops.capabilities.pinterest_post import _candidates

    targets = _candidates(conn, USER_ID, _cfg())
    assert str(LISTING_NO_IMAGE) not in targets


def test_candidates_excludes_inactive(conn):
    _seed_scenario(conn)
    from shopsteward.pipeline.ops.capabilities.pinterest_post import _candidates

    targets = _candidates(conn, USER_ID, _cfg())
    assert str(LISTING_INACTIVE) not in targets


# --- materialize() grounding + validation ------------------------------------


def test_materialize_hallucinated_target_returns_none(conn):
    _seed_scenario(conn)
    cap = SocialPinterestPost()
    action = cap.materialize(conn, USER_ID, _cfg(), _intent("999999", **_valid_params()))
    assert action is None


def test_materialize_over_length_description_dropped_not_truncated(conn):
    _seed_scenario(conn)
    cfg = _cfg()
    cap = SocialPinterestPost()
    too_long = "x" * (cfg.pinterest.max_description_len + 1)
    intent = _intent(str(LISTING_NEVER_PINNED), **_valid_params(description=too_long))
    action = cap.materialize(conn, USER_ID, cfg, intent)
    assert action is None


def test_materialize_unknown_board_key_rejected(conn):
    _seed_scenario(conn)
    cap = SocialPinterestPost()
    action = cap.materialize(
        conn,
        USER_ID,
        _cfg(),
        _intent(str(LISTING_NEVER_PINNED), **_valid_params(board_key="nonexistent_board")),
    )
    assert action is None


def test_materialize_valid_target_builds_action(conn):
    _seed_scenario(conn)
    cap = SocialPinterestPost()
    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_NEVER_PINNED), **_valid_params())
    )
    assert action is not None
    assert action.capability == "social.pinterest_post"
    assert action.target_id == str(LISTING_NEVER_PINNED)
    assert action.params["board_key"] == "wall_art"


# --- execute() -----------------------------------------------------------


def test_execute_appends_exactly_one_pin_drafted_event_with_utm(conn):
    _seed_scenario(conn)
    cap = SocialPinterestPost()
    action = cap.materialize(
        conn, USER_ID, _cfg(), _intent(str(LISTING_NEVER_PINNED), **_valid_params())
    )
    assert action is not None

    before = read_all(conn)
    result = cap.execute(conn, USER_ID, action)
    drafted = [
        e
        for e in read_all(conn, "social.pin_drafted")
        if e.payload["listing_id"] == LISTING_NEVER_PINNED
    ]

    assert len(drafted) == 1
    payload = drafted[0].payload
    assert payload["listing_id"] == LISTING_NEVER_PINNED
    assert payload["title"] == "Sandhill Cranes at Dawn"
    assert payload["board_key"] == "wall_art"

    expected_url = (
        f"https://www.etsy.com/listing/{LISTING_NEVER_PINNED}"
        f"?utm_source=pinterest&utm_medium=social&utm_campaign=shopsteward"
        f"&utm_content={action.action_id[:12]}"
    )
    assert payload["destination_url"] == expected_url
    assert action.action_id.startswith(payload["destination_url"].rsplit("utm_content=", 1)[1])

    assert result.cost_usd == 0.0
    assert len(read_all(conn)) == len(before) + 1
