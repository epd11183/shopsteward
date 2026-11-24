"""Seams for the three mockups calls pipeline code needs.

pipeline must not import shopsteward.mockups (import-linter: "mockups is imported
by no lower layer"). The composition root (shopsteward/cli.py, tests/conftest.py)
installs the real callables once; pipeline call sites fetch them with `get`.
"""

from collections.abc import Callable
from typing import Any

_HOOKS: dict[str, Callable[..., Any]] = {}
NAMES = ("run_mockups", "render_panel_jpeg", "rebuild_mockups")


def install(**fns: Callable[..., Any]) -> None:
    unknown = set(fns) - set(NAMES)
    if unknown:
        raise ValueError(f"unknown mockup hooks: {sorted(unknown)}")
    _HOOKS.update(fns)


def get(name: str) -> Callable[..., Any]:
    try:
        return _HOOKS[name]
    except KeyError:
        raise RuntimeError(
            f"mockups hook {name!r} not installed; the composition root must call "
            "shopsteward.pipeline.mockup_hooks.install()"
        ) from None
