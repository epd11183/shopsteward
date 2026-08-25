"""E2 -- one timestamp parser for the three formats the event log actually
contains: core/db.py's SQLite-default 'Z' suffix, datetime.isoformat()'s
'+00:00' suffix, and a bare ISO date (ProposedAction.expires_at)."""

from datetime import UTC, datetime, timedelta

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.pinterest_post import _candidates
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.timeutil import parse_ts
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1


def test_parse_ts_z_suffix_with_fractional_seconds():
    dt = parse_ts("2026-08-24T10:00:00.123456Z")
    assert dt == datetime(2026, 8, 24, 10, 0, 0, 123456, tzinfo=UTC)


def test_parse_ts_z_suffix_no_fractional_seconds():
    dt = parse_ts("2026-08-24T10:00:00Z")
    assert dt == datetime(2026, 8, 24, 10, 0, 0, tzinfo=UTC)


def test_parse_ts_plus_offset_suffix():
    dt = parse_ts("2026-08-24T10:00:00.123456+00:00")
    assert dt == datetime(2026, 8, 24, 10, 0, 0, 123456, tzinfo=UTC)


def test_parse_ts_bare_date_is_midnight_utc():
    dt = parse_ts("2026-08-24")
    assert dt == datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC)


def test_z_and_plus_offset_forms_of_the_same_instant_compare_equal():
    z = parse_ts("2026-08-24T10:00:00.500000Z")
    plus = parse_ts("2026-08-24T10:00:00.500000+00:00")
    assert z == plus


# --- the actual regression: cooldown comparison must use real datetimes ----


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


def test_cooldown_boundary_uses_the_real_db_created_at_format(tmp_path):
    """Insert a pin event via the real events.append() (so it gets the
    DB's own 'Z'-suffixed created_at, not a hand-crafted string), then check
    eligibility at exactly the cooldown boundary via _candidates()'s
    injectable `now` -- still cooling down AT the boundary, eligible one
    microsecond past it."""
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)

    listing_id = 501
    seed_listing_observed_on(
        conn, listing_id=listing_id, title="Loon at Dusk", day=datetime.now(UTC).date(), views=10
    )
    _seed_image(conn, listing_id)
    rebuild_core(conn)
    rebuild_ops(conn)

    pin_event = append(
        conn,
        Event(
            user_id=USER_ID,
            type="social.pin_drafted",
            payload={
                "listing_id": listing_id,
                "title": "x",
                "description": "x",
                "alt_text": "x",
                "board_key": "wall_art",
                "destination_url": "x",
                "image_url": "x",
            },
        ),
    )
    assert pin_event.created_at is not None
    assert pin_event.created_at.endswith("Z")  # the DB's own default format

    cfg = ops_config.load_ops_config()
    pinned_at = parse_ts(pin_event.created_at)
    at_boundary = pinned_at + timedelta(days=cfg.pinterest.cooldown_days)
    just_after = at_boundary + timedelta(microseconds=1)

    still_cooling = _candidates(conn, USER_ID, cfg, now=at_boundary)
    assert str(listing_id) not in still_cooling

    eligible = _candidates(conn, USER_ID, cfg, now=just_after)
    assert str(listing_id) in eligible
