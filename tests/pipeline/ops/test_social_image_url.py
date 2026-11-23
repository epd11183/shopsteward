"""S3: `social.image_url` prefers `url_fullxfull`; pre-S3 events (no such key)
still resolve to `url_570xN`."""

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.ops import social

USER_ID = 1


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed(conn, listing_id, images):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.images.observed",
            payload={"listing_id": listing_id, "images": images},
        ),
    )


def test_old_event_resolves_to_570xn(conn):
    _seed(conn, 1, [{"listing_image_id": 1, "rank": 1, "url_570xN": "https://x/570.jpg"}])
    assert social.image_url(conn, USER_ID, 1) == "https://x/570.jpg"


def test_new_event_prefers_fullxfull(conn):
    _seed(
        conn,
        1,
        [
            {
                "listing_image_id": 1,
                "rank": 1,
                "url_570xN": "https://x/570.jpg",
                "url_fullxfull": "https://x/full.jpg",
                "full_width": 3000,
                "full_height": 2000,
            }
        ],
    )
    assert social.image_url(conn, USER_ID, 1) == "https://x/full.jpg"


def test_null_fullxfull_falls_back_and_rank_wins(conn):
    _seed(
        conn,
        1,
        [
            {"listing_image_id": 2, "rank": 2, "url_570xN": "b", "url_fullxfull": "b-full"},
            {"listing_image_id": 1, "rank": 1, "url_570xN": "a", "url_fullxfull": None},
        ],
    )
    assert social.image_url(conn, USER_ID, 1) == "a"


def test_no_images_is_none(conn):
    _seed(conn, 1, [])
    assert social.image_url(conn, USER_ID, 1) is None
    assert social.image_url(conn, USER_ID, 999) is None
