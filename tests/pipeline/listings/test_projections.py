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
    )
    build_drafts(conn, USER_ID)

    row = conn.execute("SELECT * FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)).fetchone()
    assert row["state"] == "built"
    assert row["landing_file_id"] == "f" * 64
    assert row["provider"] == "etsy_digital"
    assert row["file_source"] == "landing_original"
    assert row["images_json"] != "[]"

    # rebuilding from scratch reproduces the exact same row from events alone
    rebuild_listings(conn)
    row_again = conn.execute(
        "SELECT * FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
    ).fetchone()
    assert dict(row_again) == dict(row)


def test_config_last_write_wins_on_seeded(conn):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, USER_ID)
    assert cfg.name == "default"
