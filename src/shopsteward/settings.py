"""Runtime settings. DB path via env; defaults to data/ (gitignored)."""

import os
from pathlib import Path


def db_path() -> Path:
    return Path(os.environ.get("SHOPSTEWARD_DB", "data/shopsteward.db"))
