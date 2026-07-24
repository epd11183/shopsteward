import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.projections import rebuild_listings

USER_ID = 1


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_load_listing_config_from_real_defaults_file():
    cfg = listing_config.load_listing_config()
    assert cfg.schema_version == "shopsteward.listing/1"
    assert cfg.name == "default"
    assert cfg.image_order[0] == "single"
    assert cfg.image_cap == 10
    assert cfg.etsy.sellable_max_bytes == 20_000_000


def test_config_hash_is_stable():
    cfg = listing_config.load_listing_config()
    assert listing_config.config_hash(cfg) == listing_config.config_hash(cfg)


def test_seed_from_real_defaults_file(conn):
    assert listing_config.seed(conn, USER_ID) is True


def test_seed_is_idempotent(conn):
    listing_config.seed(conn, USER_ID)
    assert listing_config.seed(conn, USER_ID) is False


def test_get_config_round_trips_after_rebuild(conn):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, USER_ID)
    assert cfg.name == "default"
    assert cfg.pricing.formats["digital_download"].base_price == 12.00


def test_get_config_missing_raises_keyerror(conn):
    rebuild_listings(conn)
    with pytest.raises(KeyError):
        listing_config.get_config(conn, USER_ID)
