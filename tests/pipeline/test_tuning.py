import json
from pathlib import Path

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.pipeline import tuning

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_seed_from_real_defaults_file(conn):
    seeded = tuning.seed(conn, user_id=1, path=DEFAULTS_PATH)
    assert seeded is True


def test_seed_is_idempotent(conn):
    tuning.seed(conn, user_id=1, path=DEFAULTS_PATH)
    second = tuning.seed(conn, user_id=1, path=DEFAULTS_PATH)
    assert second is False


def test_get_profile_round_trips_values(conn):
    tuning.seed(conn, user_id=1, path=DEFAULTS_PATH)
    profile = tuning.get_profile(conn, user_id=1, name="default")
    assert profile.vision.monthly_soft_cap_usd == 10.0
    assert profile.vision.provider == "openrouter"
    assert profile.landing.min_long_edge_px == 3000
    assert profile.schema_version == "shopsteward.tuning/1"


def test_get_profile_default_name(conn):
    tuning.seed(conn, user_id=1, path=DEFAULTS_PATH)
    profile = tuning.get_profile(conn, user_id=1)
    assert profile.name == "default"


def test_get_profile_missing_raises_keyerror(conn):
    with pytest.raises(KeyError):
        tuning.get_profile(conn, user_id=1, name="default")


def test_seed_never_overwrites_existing_profile(conn, tmp_path):
    """Once a profile name exists, seed() must not touch it — re-seeding on
    changed defaults would silently revert future operator tuning updates."""
    tuning.seed(conn, user_id=1, path=DEFAULTS_PATH)
    changed = json.loads(DEFAULTS_PATH.read_text())
    changed["vision"]["monthly_soft_cap_usd"] = 75
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(json.dumps(changed))

    seeded = tuning.seed(conn, user_id=1, path=changed_path)
    assert seeded is False
    assert tuning.get_profile(conn, user_id=1).vision.monthly_soft_cap_usd == 10.0
