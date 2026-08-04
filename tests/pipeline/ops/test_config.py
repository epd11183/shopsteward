import json

import pytest
from typer.testing import CliRunner

from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.projections import rebuild_ops

USER_ID = 1
runner = CliRunner()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_load_ops_config_from_real_defaults_file():
    cfg = ops_config.load_ops_config()
    assert cfg.schema_version == "shopsteward.ops/1"
    assert cfg.name == "default"
    assert cfg.windows.revenue_window_days == 7
    assert cfg.dead_listing.window_days == 180
    assert cfg.dead_listing.min_observed_days == 90


def test_ops_config_hash_is_stable():
    cfg = ops_config.load_ops_config()
    assert ops_config.ops_config_hash(cfg) == ops_config.ops_config_hash(cfg)


def test_ops_config_hash_differs_from_a_changed_config():
    cfg = ops_config.load_ops_config()
    changed = cfg.model_copy(update={"name": "not-default"})
    assert ops_config.ops_config_hash(cfg) != ops_config.ops_config_hash(changed)


def test_seed_from_real_defaults_file(conn):
    assert ops_config.seed(conn, USER_ID) is True


def test_seed_is_idempotent(conn):
    ops_config.seed(conn, USER_ID)
    assert ops_config.seed(conn, USER_ID) is False


def test_get_ops_config_round_trips_after_rebuild(conn):
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert cfg.name == "default"
    assert (
        cfg.shoot_more.max_listing_count
        == ops_config.load_ops_config().shoot_more.max_listing_count
    )
    assert (
        cfg.product_type_keywords.keys()
        == ops_config.load_ops_config().product_type_keywords.keys()
    )


def test_get_ops_config_missing_raises_keyerror(conn):
    rebuild_ops(conn)
    with pytest.raises(KeyError):
        ops_config.get_ops_config(conn, USER_ID)


def test_rebuild_skips_an_unknown_event_type_in_the_opsconfig_namespace(conn):
    from shopsteward.core.events import Event, append

    ops_config.seed(conn, USER_ID)
    append(conn, Event(user_id=USER_ID, type="opsconfig.disabled", payload={"reason": "test"}))

    rebuild_ops(conn)  # must not raise

    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert cfg.name == "default"


def test_rebuild_raises_keyerror_on_malformed_payload_for_a_known_event_type(conn):
    from shopsteward.core.events import Event, append

    append(conn, Event(user_id=USER_ID, type="opsconfig.updated", payload={"oops": "no name key"}))

    with pytest.raises(KeyError):
        rebuild_ops(conn)


# --- apply() (pod/config.py precedent) --------------------------------------


def _write_edited_config(tmp_path, **overrides: object):
    edited = ops_config.load_ops_config().model_dump(by_alias=True)
    edited.update(overrides)
    path = tmp_path / "edited_ops.json"
    path.write_text(json.dumps(edited))
    return path


def test_apply_seeds_when_nothing_seeded_yet(conn):
    assert ops_config.apply(conn, USER_ID) is True
    rebuild_ops(conn)
    assert ops_config.get_ops_config(conn, USER_ID).name == "default"


def test_apply_is_a_noop_when_the_file_is_unchanged(conn):
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    assert ops_config.apply(conn, USER_ID) is False


def test_seed_then_edit_then_apply_then_rebuild_reflects_the_edit(conn, tmp_path):
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    before = ops_config.get_ops_config(conn, USER_ID)
    hash_before = ops_config.ops_config_hash(before)

    edited_path = _write_edited_config(
        tmp_path,
        brief_sections={
            "revenue": False,
            "selling": True,
            "dying": True,
            "shoot_more": True,
            "data_quality": True,
        },
    )

    assert ops_config.apply(conn, USER_ID, edited_path) is True
    rebuild_ops(conn)

    after = ops_config.get_ops_config(conn, USER_ID)
    assert after.brief_sections.revenue is False
    assert ops_config.ops_config_hash(after) != hash_before


def test_ops_config_apply_cli_seeds_then_is_a_noop(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))

    first = runner.invoke(app, ["ops", "config", "apply"])
    assert first.exit_code == 0
    assert "updated" in first.output

    second = runner.invoke(app, ["ops", "config", "apply"])
    assert second.exit_code == 0
    assert "unchanged" in second.output
