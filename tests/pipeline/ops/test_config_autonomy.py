import json

from shopsteward.core.db import connect, migrate
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.projections import rebuild_ops

USER_ID = 1


def test_autonomy_block_loads_from_real_defaults_file():
    cfg = ops_config.load_ops_config()
    assert cfg.autonomy.enabled is False
    assert cfg.autonomy.monthly_spend_cap_usd == 0.00
    assert cfg.autonomy.daily_action_cap == 10
    assert cfg.autonomy.per_capability_daily_cap == 5
    assert cfg.autonomy.weekly_catalog_pct_cap == 0.10
    assert cfg.autonomy.proposal_ttl_days == 14
    assert cfg.autonomy.ladder.promote_approvals == 20
    assert cfg.autonomy.ladder.promote_min_days == 14
    assert cfg.autonomy.ladder.t1_executions == 30
    assert cfg.autonomy.ladder.t1_min_days == 30


def test_autonomy_block_round_trips_through_ops_config_hash(tmp_path):
    cfg = ops_config.load_ops_config()
    h1 = ops_config.ops_config_hash(cfg)
    h2 = ops_config.ops_config_hash(
        ops_config.OpsConfig.model_validate(cfg.model_dump(by_alias=True))
    )
    assert h1 == h2

    edited = cfg.model_dump(by_alias=True)
    edited["autonomy"]["enabled"] = True
    path = tmp_path / "edited_ops.json"
    path.write_text(json.dumps(edited))
    changed_cfg = ops_config.load_ops_config(path)
    assert ops_config.ops_config_hash(changed_cfg) != h1


def test_unchanged_ops_json_apply_is_a_noop_with_the_new_autonomy_block(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    assert ops_config.apply(conn, USER_ID) is False


def test_get_ops_config_round_trips_autonomy_after_rebuild(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert cfg.autonomy.enabled is False
    assert cfg.autonomy.monthly_spend_cap_usd == 0.00
