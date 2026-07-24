import pytest
from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings

from .helpers import USER_ID, seed_landing_file_with_mockup_set


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_rebuild_listings_creates_empty_tables(conn):
    rebuild_listings(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM proj_listing_config").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM proj_listing_drafts").fetchone()["n"] == 0


def test_rebuild_listings_is_a_true_drop_and_rebuild(conn, tmp_path):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(path, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id="f" * 64,
        photo_id="photo-1",
        path=str(path),
        set_key="set-1",
        intents=["single", "digital_whatyougot"],
        mockups_dir=tmp_path / "mockups",
    )
    build_drafts(conn, USER_ID)

    row = conn.execute("SELECT * FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)).fetchone()
    # build folds push automatically (design §1 dataflow); Fake adapter
    # always succeeds, so a fully-built draft ends this run in state=pushed.
    assert row["state"] == "pushed"
    assert row["etsy_listing_id"] is not None
    assert row["landing_file_id"] == "f" * 64
    assert row["provider"] == "etsy_digital"
    assert row["file_source"] == "landing_original"
    assert row["images_json"] != "[]"
    assert row["title"] is not None
    assert row["tags_json"] != "[]"
    assert row["description"] is not None
    assert row["price"] == 12.00
    assert row["currency"] == "USD"
    assert row["margin_floor"] == 6.00

    # rebuilding from scratch reproduces the exact same row from events alone
    rebuild_listings(conn)
    row_again = conn.execute(
        "SELECT * FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
    ).fetchone()
    assert dict(row_again) == dict(row)


def test_gate3_published_folds_state_and_published_at(conn, tmp_path):
    from shopsteward.core.events import Event, append

    path = tmp_path / "photo.jpg"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(path, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id="g" * 64,
        photo_id="photo-g",
        path=str(path),
        set_key="set-g",
        intents=["single"],
        mockups_dir=tmp_path / "mockups",
    )
    build_drafts(conn, USER_ID)
    draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
    ).fetchone()["draft_id"]

    append(
        conn,
        Event(
            user_id=USER_ID,
            type="gate3.published",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": "etsy-1",
                "state": "active",
                "published_at": "2026-07-14T00:00:00Z",
            },
        ),
    )
    rebuild_listings(conn)

    row = conn.execute(
        "SELECT state, published_at FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["state"] == "published"
    assert row["published_at"] == "2026-07-14T00:00:00Z"


def test_config_last_write_wins_on_seeded(conn):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, USER_ID)
    assert cfg.name == "default"
