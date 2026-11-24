import json

import pytest

from shopsteward.pipeline.ops import config as ops_config


@pytest.fixture(autouse=True)
def _isolated_catalog_candidates(tmp_path_factory, monkeypatch):
    """config/defaults/ops.json names the operator's real candidates folder; tests
    must never scan it. Every load_ops_config() caller (including `from` imports,
    which share the function object) gets a copy pointing at a missing folder."""
    cfg = json.loads(ops_config.OPS_CONFIG_PATH.read_text(encoding="utf-8"))
    tmp = tmp_path_factory.mktemp("opscfg")
    cfg["catalog_expansion"]["source_folder"] = str(tmp / "no_candidates")
    path = tmp / "ops.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(ops_config.load_ops_config, "__defaults__", (path,))
