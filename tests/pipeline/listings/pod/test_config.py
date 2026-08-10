import json

import pytest
from typer.testing import CliRunner

from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config

USER_ID = 1
runner = CliRunner()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_resolve_store_id_prefers_env(monkeypatch):
    monkeypatch.setenv("GELATO_STORE_ID", "real-store-999")
    cfg = pod_config.load_pod_config()
    assert pod_config.resolve_store_id(cfg) == "real-store-999"


def test_resolve_store_id_falls_back_to_config_when_env_unset(monkeypatch):
    monkeypatch.delenv("GELATO_STORE_ID", raising=False)
    cfg = pod_config.load_pod_config()
    # the committed default is the placeholder (PUBLIC repo carries no real id)
    assert pod_config.resolve_store_id(cfg) == cfg.gelato.store_id


def test_load_pod_config_from_real_defaults_file():
    cfg = pod_config.load_pod_config()
    assert cfg.schema_version == "shopsteward.pod/1"
    assert cfg.name == "default"
    assert cfg.enabled is True
    assert cfg.region == "US"
    assert "gelato" in cfg.catalog
    assert "printful" not in cfg.catalog  # dropped entirely, design §0a


def test_pod_config_hash_is_stable():
    cfg = pod_config.load_pod_config()
    assert pod_config.pod_config_hash(cfg) == pod_config.pod_config_hash(cfg)


def test_pod_config_hash_differs_from_a_changed_config():
    cfg = pod_config.load_pod_config()
    changed = cfg.model_copy(update={"name": "not-default"})
    assert pod_config.pod_config_hash(cfg) != pod_config.pod_config_hash(changed)


def test_seed_from_real_defaults_file(conn):
    assert pod_config.seed(conn, USER_ID) is True


def test_seed_is_idempotent(conn):
    pod_config.seed(conn, USER_ID)
    assert pod_config.seed(conn, USER_ID) is False


def test_get_pod_config_round_trips_after_rebuild(conn):
    pod_config.seed(conn, USER_ID)
    rebuild_pod_config(conn)
    cfg = pod_config.get_pod_config(conn, USER_ID)
    assert cfg.name == "default"
    # Compare against the file rather than a literal: markup is operator tuning
    # that changes with real supplier costs. The property under test is
    # round-trip fidelity, not the value.
    assert cfg.pricing.markup == pod_config.load_pod_config().pricing.markup
    # Nested catalog structure survives the event -> projection -> model round
    # trip. Compare shapes against the file, not hard-coded product names, so an
    # operator catalog edit does not break an unrelated round-trip test.
    from_file = pod_config.load_pod_config()
    assert cfg.catalog.keys() == from_file.catalog.keys()
    for provider, catalog_ in from_file.catalog.items():
        assert cfg.catalog[provider].products.keys() == catalog_.products.keys()
        for product_type, product in catalog_.products.items():
            round_tripped = cfg.catalog[provider].products[product_type]
            assert [v.format for v in round_tripped.variants] == [
                v.format for v in product.variants
            ]
            assert [v.orientation for v in round_tripped.variants] == [
                v.orientation for v in product.variants
            ]


def test_get_pod_config_missing_raises_keyerror(conn):
    rebuild_pod_config(conn)
    with pytest.raises(KeyError):
        pod_config.get_pod_config(conn, USER_ID)


def test_rebuild_skips_an_unknown_event_type_in_the_podconfig_namespace(conn):
    # design §15's rollback lever invites a future podconfig.disabled event;
    # the fold must not KeyError indexing payload["name"]/["config"] on a
    # shape it doesn't recognise yet.
    from shopsteward.core.events import Event, append

    pod_config.seed(conn, USER_ID)
    append(conn, Event(user_id=USER_ID, type="podconfig.disabled", payload={"reason": "test"}))

    rebuild_pod_config(conn)  # must not raise

    cfg = pod_config.get_pod_config(conn, USER_ID)
    assert cfg.name == "default"


def test_rebuild_raises_keyerror_on_malformed_payload_for_a_known_event_type(conn):
    # the reachable KeyError case: rebuild_pod_config's guard filters by
    # event TYPE only, so a malformed payload on a type it DOES recognise
    # (podconfig.updated) still blows up indexing payload["name"].
    from shopsteward.core.events import Event, append

    append(conn, Event(user_id=USER_ID, type="podconfig.updated", payload={"oops": "no name key"}))

    with pytest.raises(KeyError):
        rebuild_pod_config(conn)


def test_pod_config_is_a_separate_file_from_listing_config():
    # design §2: pod.json is a SEPARATE config file with its own hash --
    # folding it into listing.json would change config_hash() and orphan
    # every existing digital draft_id.
    from shopsteward.pipeline.listings import config as listing_config

    assert pod_config.POD_CONFIG_PATH != listing_config.LISTING_CONFIG_PATH
    assert pod_config.POD_CONFIG_PATH.name == "pod.json"


# --- apply() (review fix-up C: seed() alone has no rollback path) ---------


def _write_edited_config(tmp_path, **overrides: object):
    edited = pod_config.load_pod_config().model_dump(by_alias=True)
    edited.update(overrides)
    path = tmp_path / "edited_pod.json"
    path.write_text(json.dumps(edited))
    return path


def test_apply_seeds_when_nothing_seeded_yet(conn):
    assert pod_config.apply(conn, USER_ID) is True
    rebuild_pod_config(conn)
    assert pod_config.get_pod_config(conn, USER_ID).name == "default"


def test_apply_is_a_noop_when_the_file_is_unchanged(conn):
    pod_config.seed(conn, USER_ID)
    rebuild_pod_config(conn)
    assert pod_config.apply(conn, USER_ID) is False


def test_seed_then_edit_then_apply_then_rebuild_reflects_the_edit(conn, tmp_path):
    pod_config.seed(conn, USER_ID)
    rebuild_pod_config(conn)
    before = pod_config.get_pod_config(conn, USER_ID)
    hash_before = pod_config.pod_config_hash(before)

    edited_path = _write_edited_config(tmp_path, enabled=False)

    assert pod_config.apply(conn, USER_ID, edited_path) is True
    rebuild_pod_config(conn)

    after = pod_config.get_pod_config(conn, USER_ID)
    assert after.enabled is False
    assert pod_config.pod_config_hash(after) != hash_before


def test_pod_config_hash_diverges_if_the_file_is_edited_without_apply(conn, tmp_path):
    # pin: a consumer must hash the PodConfig object it actually reads (the
    # DB row via get_pod_config()), never re-derive the hash from the file
    # while other code reads the DB -- editing the file with no `pod config
    # apply` leaves the two silently divergent.
    pod_config.seed(conn, USER_ID)
    rebuild_pod_config(conn)
    hash_from_db = pod_config.pod_config_hash(pod_config.get_pod_config(conn, USER_ID))

    edited_path = _write_edited_config(tmp_path, enabled=False)
    hash_from_file = pod_config.pod_config_hash(pod_config.load_pod_config(edited_path))

    assert hash_from_file != hash_from_db


def test_pod_config_apply_cli_seeds_then_is_a_noop(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))

    first = runner.invoke(app, ["pod", "config", "apply"])
    assert first.exit_code == 0
    assert "updated" in first.output

    second = runner.invoke(app, ["pod", "config", "apply"])
    assert second.exit_code == 0
    assert "unchanged" in second.output
