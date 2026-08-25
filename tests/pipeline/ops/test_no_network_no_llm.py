"""Structural + behavioural proof that the DETERMINISTIC brief (M8a slice 1,
design §7's "the brief is SQL and a template, not a language model") makes
zero network calls and zero llm.call events.

Static: none of pipeline/ops's deterministic-only source files import httpx,
requests, or any adapters package -- there is no transport to call out over
in the first place, so there is nothing to fake in a test. `planner.py` and
`cli.py` are exempt: M8b slice 1 (design §5) intentionally adds a narrated
Brief -- planner.py calls the PlannerAdapter (network, gated on
live_planner_open()) and cli.py constructs the live adapter for `--narrate`.
The deterministic generate_brief()/render_text() in brief.py itself stays
untouched by that addition, which the behavioural test below still proves.
`registry.py` is exempt too (M8b slice 2, design §2): it imports
`adapters.planner.interface.ProposalIntent` for the `Capability.materialize()`
type hint only -- a pure-Pydantic boundary shape with zero httpx/transport
of its own, not a network call. `keyword_probe.py` is exempt for the same
reason (2026-08-25 Etsy keyword/competition probe): it imports the
`EtsyAdapter` Protocol and `EtsyActiveListingResult` Pydantic model purely
for type hints -- it never imports `LiveEtsyAdapter`/httpx itself, and the
transport call actually happens through whatever adapter its caller (CLI)
passes in, exactly like every other `EtsyAdapter`-typed function in this
codebase. The import-linter contract (pyproject.toml) is the actual
one-way-import enforcement; this test's job is narrower -- proving no ops
module reaches for a transport it doesn't need.

Behavioural: after seeding a synthetic shop and generating the full brief,
the event log contains only the event types this slice is allowed to
produce (etsy.*.observed from the fixture seed + opsconfig.seeded) -- no
llm.call, no action.*, no capability.*."""

import ast
from pathlib import Path

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import brief as brief_module
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import AS_OF, USER_ID, seed_two_year_shop

_OPS_DIR = Path(brief_module.__file__).parent
_FORBIDDEN_MODULES = ("httpx", "requests", "shopsteward.adapters")
_NARRATION_FILES = {"planner.py", "cli.py", "registry.py", "keyword_probe.py"}


def _imported_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "py_file",
    sorted(
        p
        for p in _OPS_DIR.glob("*.py")
        if p.name != "__pycache__" and p.name not in _NARRATION_FILES
    ),
)
def test_ops_module_imports_no_network_or_adapter_transport(py_file):
    imported = _imported_modules(py_file)
    for forbidden in _FORBIDDEN_MODULES:
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in imported), (
            f"{py_file.name} imports {forbidden!r} -- slice 1 must make zero network calls"
        )


def test_generating_the_brief_appends_no_llm_or_network_adjacent_events(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    seed_two_year_shop(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)

    brief_module.generate_brief(conn, USER_ID, cfg, as_of=AS_OF)

    types = {e.type for e in read_all(conn)}
    assert "llm.call" not in types
    assert not any(t.startswith("action.") or t.startswith("capability.") for t in types)
    assert types <= {"etsy.listing.observed", "etsy.sale.observed", "opsconfig.seeded"}
