"""config/defaults/mockups.json loads + validates, and config_hash is stable."""

from shopsteward.mockups.config import config_hash, load_mockup_defaults
from shopsteward.mockups.models import MockupConfig


def test_load_mockup_defaults_validates():
    cfg = load_mockup_defaults()
    assert isinstance(cfg, MockupConfig)
    assert cfg.schema_version == "shopsteward.mockups/1"
    assert cfg.intents["single"].count == 2
    assert cfg.render.output_long_edge_px == 2400
    assert cfg.products.default_print_widths_inches["landscape"] == 24


def test_config_hash_is_stable():
    cfg = load_mockup_defaults()
    h1 = config_hash(cfg)
    h2 = config_hash(load_mockup_defaults())
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest


def test_config_hash_changes_with_content():
    cfg = load_mockup_defaults()
    h1 = config_hash(cfg)
    mutated = cfg.model_copy(update={"render": cfg.render.model_copy(update={"jpeg_quality": 50})})
    h2 = config_hash(mutated)
    assert h1 != h2
