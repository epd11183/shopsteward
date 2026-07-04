"""Ensure the src/ layout package is importable without a packaged install.

The project is not yet configured with a [build-system] in pyproject.toml,
so `uv sync` does not install `shopsteward` as an editable package. Until
that packaging config lands, tests need src/ on sys.path directly.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
