"""Path to shippable mockup defaults + a tiny loader/hasher for mockups.json."""

import hashlib
import json
from pathlib import Path

from shopsteward.mockups.models import MockupConfig

_REPO_ROOT = Path(__file__).resolve().parents[3]
MOCKUP_DEFAULTS_PATH = _REPO_ROOT / "config" / "defaults" / "mockups.json"


def load_mockup_defaults() -> MockupConfig:
    return MockupConfig.model_validate_json(MOCKUP_DEFAULTS_PATH.read_text())


def config_hash(cfg: MockupConfig) -> str:
    canonical = json.dumps(cfg.model_dump(by_alias=True), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
