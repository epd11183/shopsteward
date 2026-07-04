from pathlib import Path

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.editing import presets

DEFAULTS_DIR = Path(__file__).parents[2] / "config" / "defaults" / "preset_families"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_seed_from_real_defaults_dir(conn):
    count = presets.seed(conn, user_id=1, defaults_dir=DEFAULTS_DIR)
    assert count == 4
    families = presets.list_families(conn, user_id=1)
    assert {f.name for f in families} == {"neutral", "wedding", "race", "brewery"}


def test_seed_is_idempotent(conn):
    presets.seed(conn, user_id=1, defaults_dir=DEFAULTS_DIR)
    second_count = presets.seed(conn, user_id=1, defaults_dir=DEFAULTS_DIR)
    assert second_count == 0


def test_get_family_returns_settings(conn):
    presets.seed(conn, user_id=1, defaults_dir=DEFAULTS_DIR)
    wedding = presets.get_family(conn, user_id=1, name="wedding")
    assert wedding.settings["Vibrance"] == 18


def test_get_family_unknown_raises_with_available_names(conn):
    presets.seed(conn, user_id=1, defaults_dir=DEFAULTS_DIR)
    with pytest.raises(KeyError) as exc_info:
        presets.get_family(conn, user_id=1, name="nope")
    message = str(exc_info.value)
    assert "nope" in message
    assert "wedding" in message


def test_seed_reseeds_when_settings_change(conn, tmp_path):
    presets.seed(conn, user_id=1, defaults_dir=DEFAULTS_DIR)
    changed_dir = tmp_path / "changed"
    changed_dir.mkdir()
    (changed_dir / "wedding.json").write_text(
        '{"name": "wedding", "description": "updated", "settings": {"Vibrance": 99}}'
    )
    count = presets.seed(conn, user_id=1, defaults_dir=changed_dir)
    assert count == 1
    assert presets.get_family(conn, user_id=1, name="wedding").settings["Vibrance"] == 99
