"""The CLI root must install every mockups hook. Fresh interpreter: tests/conftest.py
installs them for the whole suite, so an in-process check would always pass."""

import subprocess
import sys


def test_cli_import_installs_every_mockup_hook():
    code = (
        "import shopsteward.cli\n"
        "from shopsteward.pipeline import mockup_hooks as h\n"
        "assert set(h._HOOKS) == set(h.NAMES), sorted(h._HOOKS)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
