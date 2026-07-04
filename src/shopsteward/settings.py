"""Runtime settings. DB path via env; defaults to data/ (gitignored)."""

import os
from pathlib import Path

DEFAULT_USER_ID = 1  # single-operator v1; schema stays multi-tenant-ready


def db_path() -> Path:
    return Path(os.environ.get("SHOPSTEWARD_DB", "data/shopsteward.db"))


def bridge_dir() -> Path:
    return Path(os.environ.get("SHOPSTEWARD_BRIDGE_DIR", "data/bridge"))


def landing_dir() -> Path:
    return Path(os.environ.get("SHOPSTEWARD_LANDING_DIR", "data/landing"))
