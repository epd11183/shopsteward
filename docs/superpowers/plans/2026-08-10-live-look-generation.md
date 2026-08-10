# Live Look Generation + Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire live (Claude-via-OpenRouter) look generation into `shopsteward edit`, gated + cost-capped, with a deterministic sanity guard and an A/B `look preview` command.

**Architecture:** All new code is editing-module-local (import-linter forbids `editing` → `pipeline`). The `adapters/look` OpenRouter adapter already exists; this effort adds the gate, config, cost ledger, sanity guard, resolution wiring, and CLI surface around it. Event-sourced cost ledger via `core.events`.

**Tech Stack:** Python 3.12, Pydantic v2, Typer, `httpx`/`respx` (OpenRouter, mocked in tests), SQLite event log. No live APIs in tests; live path gated off by default.

**Spec:** `docs/superpowers/specs/2026-08-10-live-look-generation-design.md`

---

## Design decisions locked in
- Live generation gated by `SHOPSTEWARD_LIVE_LOOK=1` + `OPENROUTER_API_KEY`; `--live-look` CLI flag. Passed-but-closed → refuse (no silent fixture fallback).
- Only a **new described** look calls the LLM; named/saved looks never do.
- Sanity guard runs **only on LLM-generated** looks (seeds are trusted). Fail → regenerate once → fall back to a config seed.
- Soft cap is a **hard refuse** when the month's look-LLM spend ≥ cap.
- Model: a **Claude** model via OpenRouter (id + pricing pinned in Task 1, surfaced for operator OK).

## File structure
Created: `editing/live_look.py`, `editing/look_guard.py`, `editing/look_cost.py`, tests under `tests/editing/`.
Modified: `config/defaults/editing.json` (+`look_llm`, +`look_guard`), `editing/config.py` (+accessors), `editing/looks.py` (`resolve_look` gains guard+cost+fallback), `editing/edit.py` (`run_edit` threads the new config), `editing/cli.py` (`--live-look` + `look preview`).

---

## Task 1: Config blocks + Claude model pin

**Files:**
- Modify: `config/defaults/editing.json`
- Modify: `src/shopsteward/editing/config.py`
- Test: `tests/editing/test_look_config.py`

- [ ] **Step 1: Pin the Claude model id + pricing**

Use the `find-docs` skill (query "OpenRouter Claude model ids structured outputs json_schema") and the `claude-api` skill to confirm: (a) the current Claude Sonnet OpenRouter model slug (expected form `anthropic/claude-sonnet-...`), (b) its input/output per-Mtok pricing, and (c) whether OpenRouter honors `response_format: json_schema strict` for Claude (if not, note that Task 5's live smoke must use the prompt-guided fallback). Record the confirmed id + pricing; use them in Step 2. **Surface the chosen model + pricing to the operator for sign-off before proceeding** (AI model/provider selection is an operator-review item).

- [ ] **Step 2: Add config blocks**

In `config/defaults/editing.json`, add two top-level blocks (sibling to `correction`), using the pinned id + pricing from Step 1 (values below are placeholders — replace `<model>` and the pricing numbers):

```json
  "look_llm": {
    "provider": "openrouter",
    "model": "<model>",
    "temperature": 0.7,
    "pricing": { "<model>": { "in": 0.0, "out": 0.0 } },
    "monthly_soft_cap_usd": 5.0
  },
  "look_guard": {
    "fallback_look": "bright-and-true",
    "max_saturation_load": 220,
    "max_contrast_tone": 140,
    "max_presence_load": 200,
    "max_split_saturation": 60
  }
```

- [ ] **Step 3: Add config accessors**

Append to `src/shopsteward/editing/config.py`:

```python
def load_look_llm() -> dict:
    return load_editing_defaults().get("look_llm", {})


def load_look_guard() -> dict:
    return load_editing_defaults().get("look_guard", {})
```

- [ ] **Step 4: Write + run the test**

`tests/editing/test_look_config.py`:

```python
from shopsteward.editing import config


def test_look_llm_block_present():
    llm = config.load_look_llm()
    assert llm["provider"] == "openrouter"
    assert llm["model"]
    assert llm["monthly_soft_cap_usd"] > 0


def test_look_guard_block_present():
    g = config.load_look_guard()
    assert g["fallback_look"] == "bright-and-true"
    assert g["max_saturation_load"] > 0
```

Run: `uv run pytest tests/editing/test_look_config.py -v` → 2 passed.

- [ ] **Step 5: Commit**

```bash
git add config/defaults/editing.json src/shopsteward/editing/config.py tests/editing/test_look_config.py
git commit -m "feat(editing): add look_llm + look_guard config blocks and accessors"
```

---

## Task 2: Live-look gate

**Files:**
- Create: `src/shopsteward/editing/live_look.py`
- Test: `tests/editing/test_live_look.py`

- [ ] **Step 1: Write the failing test**

`tests/editing/test_live_look.py`:

```python
from shopsteward.editing.live_look import live_look_error, live_look_open


def test_gate_closed_without_flag(monkeypatch):
    monkeypatch.delenv("SHOPSTEWARD_LIVE_LOOK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert live_look_open() is False


def test_gate_closed_without_key(monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_LIVE_LOOK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert live_look_open() is False


def test_gate_open_with_both(monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_LIVE_LOOK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert live_look_open() is True


def test_error_names_flag_and_env():
    msg = live_look_error()
    assert "SHOPSTEWARD_LIVE_LOOK" in msg and "OPENROUTER_API_KEY" in msg and "--live-look" in msg
```

- [ ] **Step 2: Run → FAIL (ImportError).**

- [ ] **Step 3: Implement**

`src/shopsteward/editing/live_look.py`:

```python
"""Editing-local live-look gate. The editing module cannot import
pipeline.live_gate (import-linter), so it mirrors that convention here: live
Claude-via-OpenRouter look generation requires an explicit flag + the API key."""

import os


def live_look_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_LOOK=1 and OPENROUTER_API_KEY are both set."""
    return os.environ.get("SHOPSTEWARD_LIVE_LOOK") == "1" and bool(
        os.environ.get("OPENROUTER_API_KEY")
    )


def live_look_error() -> str:
    return (
        "Live look generation is gated on operator approval: set "
        "SHOPSTEWARD_LIVE_LOOK=1 and OPENROUTER_API_KEY in the environment, "
        "then re-run with --live-look."
    )
```

- [ ] **Step 4: Run → 4 passed. Commit**

```bash
git add src/shopsteward/editing/live_look.py tests/editing/test_live_look.py
git commit -m "feat(editing): add editing-local live-look gate"
```

---

## Task 3: Deterministic sanity guard

**Files:**
- Create: `src/shopsteward/editing/look_guard.py`
- Test: `tests/editing/test_look_guard.py`

- [ ] **Step 1: Write the failing test**

`tests/editing/test_look_guard.py`:

```python
from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.look_guard import sanitize_look

KNOBS = {
    "max_saturation_load": 220, "max_contrast_tone": 140,
    "max_presence_load": 200, "max_split_saturation": 60,
}


def test_tasteful_look_passes():
    lp = LookProfile(name="x", contrast=18, vibrance=14, saturation=-4,
                     tone_curve=[[0, 6], [128, 128], [255, 250]])
    assert sanitize_look(lp, KNOBS).ok is True


def test_oversaturated_look_rejected():
    lp = LookProfile(name="x", vibrance=100, saturation=100,
                     hsl={"SaturationAdjustmentOrange": 100, "SaturationAdjustmentBlue": 100})
    v = sanitize_look(lp, KNOBS)
    assert v.ok is False and "saturation" in v.reason.lower()


def test_extreme_presence_rejected():
    lp = LookProfile(name="x", clarity=100, dehaze=100, texture=100, whites=50, blacks=-50)
    assert sanitize_look(lp, KNOBS).ok is False


def test_harsh_split_tone_rejected():
    lp = LookProfile(name="x", split_toning={"SplitToningShadowSaturation": 90})
    assert sanitize_look(lp, KNOBS).ok is False
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`src/shopsteward/editing/look_guard.py`:

```python
"""Deterministic taste guard for LLM-generated looks. Hand-authored seed looks
are trusted and skip this; a freshly generated look that trips a bounded
aggregate check is rejected (caller retries once, then falls back to a seed).
Thresholds are calibration knobs. ponytail: heuristic aggregate caps, tune the
knobs if they reject good looks or pass garish ones."""

from pydantic import BaseModel

from shopsteward.adapters.look.interface import LookProfile


class LookVerdict(BaseModel):
    ok: bool
    reason: str | None = None


def _tone_steepness(points: list[list[int]]) -> int:
    return max((abs(int(y) - int(x)) for x, y in points if True), default=0) if points else 0


def sanitize_look(profile: LookProfile, knobs: dict) -> LookVerdict:
    sat_load = abs(profile.vibrance) + abs(profile.saturation)
    sat_load += sum(abs(v) for k, v in profile.hsl.items() if "Saturation" in k)
    sat_load += sum(abs(v) for k, v in profile.split_toning.items() if k.endswith("Saturation"))
    if sat_load > knobs.get("max_saturation_load", 220):
        return LookVerdict(ok=False, reason=f"saturation load {sat_load} over cap")

    contrast_tone = abs(profile.contrast) + _tone_steepness(profile.tone_curve)
    if contrast_tone > knobs.get("max_contrast_tone", 140):
        return LookVerdict(ok=False, reason=f"contrast+tone {contrast_tone} over cap")

    presence = (abs(profile.clarity) + abs(profile.dehaze) + abs(profile.texture)
                + abs(profile.highlights) + abs(profile.whites) + abs(profile.blacks))
    if presence > knobs.get("max_presence_load", 200):
        return LookVerdict(ok=False, reason=f"presence load {presence} over cap")

    cap = knobs.get("max_split_saturation", 60)
    for k, v in profile.split_toning.items():
        if k.endswith("Saturation") and v > cap:
            return LookVerdict(ok=False, reason=f"split-tone {k}={v} over cap")

    return LookVerdict(ok=True, reason=None)
```

- [ ] **Step 4: Run → 4 passed. Commit**

```bash
git add src/shopsteward/editing/look_guard.py tests/editing/test_look_guard.py
git commit -m "feat(editing): add deterministic look sanity guard"
```

---

## Task 4: Cost ledger + monthly soft cap

**Files:**
- Create: `src/shopsteward/editing/look_cost.py`
- Test: `tests/editing/test_look_cost.py`

The month key comes from event `created_at` (SQLite `CURRENT_TIMESTAMP`, `YYYY-MM-DD...`), so `month_look_cost` sums events whose `created_at` starts with a given `YYYY-MM` prefix. The caller passes the current month prefix (avoids `Date.now()` in library code; the CLI supplies it).

- [ ] **Step 1: Write the failing test**

`tests/editing/test_look_cost.py`:

```python
from shopsteward.adapters.look.interface import LookUsage
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.editing.look_cost import append_llm_call, month_look_cost

USER = 1


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def test_append_and_sum_current_month():
    c = _conn()
    append_llm_call(c, USER, LookUsage(model="m", input_tokens=10, output_tokens=20,
                                       est_cost_usd=0.03), description="a look")
    append_llm_call(c, USER, LookUsage(model="m", est_cost_usd=0.05), description="b look")
    # created_at is CURRENT_TIMESTAMP; derive its month prefix from the event itself.
    month = read_all(c, "llm.call")[0].created_at[:7]
    assert round(month_look_cost(c, USER, month), 2) == 0.08


def test_other_month_excluded():
    c = _conn()
    append_llm_call(c, USER, LookUsage(model="m", est_cost_usd=1.0), description="x")
    assert month_look_cost(c, USER, "1999-01") == 0.0


def test_none_cost_counts_as_zero():
    c = _conn()
    append_llm_call(c, USER, LookUsage(model="m", est_cost_usd=None), description="x")
    month = read_all(c, "llm.call")[0].created_at[:7]
    assert month_look_cost(c, USER, month) == 0.0
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

`src/shopsteward/editing/look_cost.py`:

```python
"""Editing-local, event-sourced cost ledger for look generation. Cannot use
pipeline.llm_ledger (import-linter), so it appends `llm.call` events to the core
log and sums them by month. The month prefix (YYYY-MM) is supplied by the caller
to keep this library free of wall-clock reads."""

import sqlite3

from shopsteward.adapters.look.interface import LookUsage
from shopsteward.core.events import Event, append, read_all

_LOOK_LLM_TYPE = "llm.call"


def append_llm_call(
    conn: sqlite3.Connection, user_id: int, usage: LookUsage, *, description: str
) -> None:
    append(conn, Event(user_id=user_id, type=_LOOK_LLM_TYPE, payload={
        "feature": "look",
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "est_cost_usd": usage.est_cost_usd,
        "description": description,
    }))


def month_look_cost(conn: sqlite3.Connection, user_id: int, month_prefix: str) -> float:
    total = 0.0
    for e in read_all(conn, _LOOK_LLM_TYPE):
        if (e.user_id == user_id and e.payload.get("feature") == "look"
                and (e.created_at or "").startswith(month_prefix)):
            total += e.payload.get("est_cost_usd") or 0.0
    return total
```

- [ ] **Step 4: Run → 3 passed. Commit**

```bash
git add src/shopsteward/editing/look_cost.py tests/editing/test_look_cost.py
git commit -m "feat(editing): add editing-local look-LLM cost ledger"
```

---

## Task 5: Wire guard + cost + fallback into look resolution

**Files:**
- Modify: `src/shopsteward/editing/looks.py`
- Test: `tests/editing/test_looks_gated.py`

Extend `resolve_look` with keyword-only optional params so existing callers are unaffected. New behavior applies only in the **LLM-generate** branch.

- [ ] **Step 1: Write the failing test**

`tests/editing/test_looks_gated.py`:

```python
import pytest

from shopsteward.adapters.look.fake import FakeLookAdapter
from shopsteward.adapters.look.interface import LookProfile, LookResult, LookUsage
from shopsteward.core.db import connect, migrate
from shopsteward.editing import looks
from shopsteward.editing.config import LOOKS_DIR
from shopsteward.editing.look_cost import month_look_cost

USER = 1
GUARD = {"max_saturation_load": 220, "max_contrast_tone": 140,
         "max_presence_load": 200, "max_split_saturation": 60}


def _conn():
    c = connect(":memory:")
    migrate(c)
    looks.seed(c, USER, LOOKS_DIR)  # for the fallback seed
    return c


def _garish():
    return LookResult(profile=LookProfile(name="g", vibrance=100, saturation=100,
        hsl={"SaturationAdjustmentOrange": 100, "SaturationAdjustmentBlue": 100}),
        usage=LookUsage(model="m", est_cost_usd=0.02))


def _tasteful():
    return LookResult(profile=LookProfile(name="t", contrast=15, vibrance=10),
                      usage=LookUsage(model="m", est_cost_usd=0.02))


def test_garish_generation_falls_back_to_seed_after_retry():
    c = _conn()
    adapter = FakeLookAdapter([_garish(), _garish()])  # both fail the guard
    out = looks.resolve_look(c, USER, "loud look", adapter, model="m", regenerate=False,
                             guard_knobs=GUARD, fallback_look="bright-and-true",
                             month_prefix="2026-08")
    assert out.name == "bright-and-true"  # fell back
    assert len(adapter.calls) == 2  # generated once, retried once


def test_tasteful_generation_is_kept_and_ledgered():
    c = _conn()
    adapter = FakeLookAdapter([_tasteful()])
    out = looks.resolve_look(c, USER, "nice look", adapter, model="m", regenerate=False,
                             guard_knobs=GUARD, month_prefix="2026-08", pricing={})
    assert out.contrast == 15
    assert month_look_cost(c, USER, "2026-08") == 0.02  # llm.call emitted


def test_soft_cap_refuses_before_generating():
    c = _conn()
    # Pre-seed spend at the cap.
    from shopsteward.adapters.look.interface import LookUsage as U
    from shopsteward.editing.look_cost import append_llm_call
    append_llm_call(c, USER, U(model="m", est_cost_usd=5.0), description="prior")
    adapter = FakeLookAdapter([_tasteful()])
    with pytest.raises(looks.LookCostCapError):
        looks.resolve_look(c, USER, "new look", adapter, model="m", regenerate=False,
                           guard_knobs=GUARD, month_prefix="2026-08",
                           soft_cap_usd=5.0)
    assert adapter.calls == []  # never called the LLM
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement — extend `resolve_look` and add `LookCostCapError`**

Add near the top of `src/shopsteward/editing/looks.py` (after imports):

```python
from shopsteward.editing.look_cost import append_llm_call, month_look_cost
from shopsteward.editing.look_guard import sanitize_look


class LookCostCapError(RuntimeError):
    """Raised when the month's look-LLM spend is already at/over the soft cap."""
```

Replace the generate branch of `resolve_look` (currently: `result = adapter.generate_look(...)`, `profile = result.profile.model_copy(...)`, `save_look(...)`, `return profile`) and widen the signature. The full new `resolve_look`:

```python
def resolve_look(
    conn: sqlite3.Connection,
    user_id: int,
    look_arg: str,
    adapter: LookAdapter,
    *,
    model: str,
    regenerate: bool,
    guard_knobs: dict | None = None,
    soft_cap_usd: float | None = None,
    pricing: dict | None = None,
    fallback_look: str = "bright-and-true",
    month_prefix: str | None = None,
) -> LookProfile:
    """Resolve --look: an exact stored name wins; otherwise treat as a description
    keyed by normalized text (reload unless --regenerate); else generate + save.
    When generating: enforce the monthly soft cap, run the sanity guard
    (retry once, then fall back to a seed), and ledger the LLM cost."""
    latest = _latest_by_name(conn, user_id)
    if look_arg in latest:
        return _profile_from_payload(latest[look_arg])

    key = _desc_key(look_arg)
    if not regenerate and key in latest:
        return _profile_from_payload(latest[key])

    # Soft cap: refuse BEFORE spending, if we can price calls at all.
    if soft_cap_usd is not None and month_prefix is not None:
        if month_look_cost(conn, user_id, month_prefix) >= soft_cap_usd:
            raise LookCostCapError(
                f"look-LLM spend for {month_prefix} is at the ${soft_cap_usd} cap; "
                "raise look_llm.monthly_soft_cap_usd to continue"
            )

    profile = _generate_gated(conn, user_id, look_arg, key, adapter, model,
                              guard_knobs, fallback_look)
    save_look(conn, user_id, profile)
    return profile


def _generate_gated(conn, user_id, look_arg, key, adapter, model, guard_knobs, fallback_look):
    for attempt in range(2):  # generate, then one retry on a guard failure
        result = adapter.generate_look(look_arg, model=model)
        if result.usage is not None:
            append_llm_call(conn, user_id, result.usage, description=look_arg)
        candidate = result.profile.model_copy(update={"name": key, "description": look_arg})
        if guard_knobs is None or sanitize_look(candidate, guard_knobs).ok:
            return candidate
    # Both attempts tripped the guard -> fall back to a trusted seed look.
    return get_look(conn, user_id, fallback_look)
```

Notes: `get_look`/`save_look`/`_desc_key`/`_profile_from_payload` already exist. The offline `FixtureLookAdapter` returns `usage=None` and (in existing tests) `guard_knobs=None`, so its behavior is unchanged.

- [ ] **Step 4: Run → 3 passed. Also run existing `test_looks.py`**

Run: `uv run pytest tests/editing/test_looks.py tests/editing/test_looks_gated.py -v` → all pass (existing resolve_look tests unaffected: they pass no guard_knobs/soft_cap).

- [ ] **Step 5: Commit**

```bash
git add src/shopsteward/editing/looks.py tests/editing/test_looks_gated.py
git commit -m "feat(editing): gate look generation (guard + soft cap + fallback)"
```

---

## Task 6: Thread the config through `run_edit`

**Files:**
- Modify: `src/shopsteward/editing/edit.py`
- Test: `tests/editing/test_edit_gated.py`

`run_edit` calls `looks.resolve_look` internally; add optional params it forwards.

- [ ] **Step 1: Write the failing test**

`tests/editing/test_edit_gated.py`:

```python
from pathlib import Path

import numpy as np

from shopsteward.adapters.look.fake import FakeLookAdapter
from shopsteward.adapters.look.interface import LookProfile, LookResult, LookUsage
from shopsteward.core.db import connect, migrate
from shopsteward.editing.edit import run_edit
from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

USER = 1
KNOBS = {"exposure_target_luma": 0.4, "exposure_max_stops": 1.5, "shadow_trigger_luma": 0.12,
         "shadow_lift_max": 0.8, "shadow_range_low": 0, "shadow_range_high": 35,
         "cast_trigger": 0.06, "cast_nudge_cap": 8, "cast_full_scale_bias": 0.2}
GUARD = {"max_saturation_load": 220, "max_contrast_tone": 140,
         "max_presence_load": 200, "max_split_saturation": 60}


def test_run_edit_forwards_guard_and_ledgers(tmp_path: Path):
    raw = tmp_path / "A.CR3"
    raw.write_bytes(b"a")
    decoder = FakeRawDecoder({str(raw): DecodedImage(rgb=np.full((8, 8, 3), 0.3, np.float32))})
    adapter = FakeLookAdapter([LookResult(
        profile=LookProfile(name="t", contrast=15), usage=LookUsage(model="m", est_cost_usd=0.01))])
    conn = connect(":memory:")
    migrate(conn)
    report = run_edit(conn, USER, tmp_path, "some described look",
                      decoder=decoder, look_adapter=adapter, model="m", knobs=KNOBS,
                      regenerate=False, overwrite=False, batch_lock=False,
                      guard_knobs=GUARD, soft_cap_usd=5.0, pricing={}, month_prefix="2026-08")
    assert report.written == 1
    from shopsteward.editing.look_cost import month_look_cost
    assert month_look_cost(conn, USER, "2026-08") == 0.01
```

- [ ] **Step 2: Run → FAIL (unexpected kwargs).**

- [ ] **Step 3: Implement**

In `src/shopsteward/editing/edit.py`, add these keyword-only params to `run_edit` (after `batch_lock`):

```python
    guard_knobs: dict | None = None,
    soft_cap_usd: float | None = None,
    pricing: dict | None = None,
    fallback_look: str = "bright-and-true",
    month_prefix: str | None = None,
```

And forward them in the `looks.resolve_look(...)` call:

```python
    look = looks.resolve_look(
        conn, user_id, look_arg, look_adapter, model=model, regenerate=regenerate,
        guard_knobs=guard_knobs, soft_cap_usd=soft_cap_usd, pricing=pricing,
        fallback_look=fallback_look, month_prefix=month_prefix,
    )
```

- [ ] **Step 4: Run → pass. Also run `test_edit.py` (unchanged behavior). Commit**

```bash
git add src/shopsteward/editing/edit.py tests/editing/test_edit_gated.py
git commit -m "feat(editing): thread look guard/cost config through run_edit"
```

---

## Task 7: CLI `--live-look` wiring

**Files:**
- Modify: `src/shopsteward/editing/cli.py`
- Test: `tests/editing/test_cli_live_look.py`

Build the live adapter only when `--live-look` is set and the gate is open; refuse if passed-but-closed. The CLI supplies `month_prefix` (its one wall-clock read).

- [ ] **Step 1: Write the failing test**

`tests/editing/test_cli_live_look.py`:

```python
from pathlib import Path

from typer.testing import CliRunner

from shopsteward.editing.cli import edit_app

runner = CliRunner()


def test_live_look_refused_when_gate_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_LOOK", raising=False)
    (tmp_path / "IMG.CR3").write_bytes(b"x")
    result = runner.invoke(edit_app, ["run", str(tmp_path), "--look", "brand new look", "--live-look"])
    assert result.exit_code != 0
    assert "SHOPSTEWARD_LIVE_LOOK" in result.output


def test_named_look_offline_still_works(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    (tmp_path / "IMG.CR3").write_bytes(b"x")
    import numpy as np
    from shopsteward.editing import cli as cli_mod
    from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder
    fake = FakeRawDecoder({str(tmp_path / "IMG.CR3"): DecodedImage(rgb=np.full((8, 8, 3), 0.2, np.float32))})
    monkeypatch.setattr(cli_mod, "_default_decoder", lambda: fake)
    result = runner.invoke(edit_app, ["run", str(tmp_path), "--look", "bright-and-true"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

In `src/shopsteward/editing/cli.py`, add imports:

```python
from datetime import datetime, timezone

from shopsteward.adapters.look.openrouter import OpenRouterLookAdapter
from shopsteward.editing.config import load_look_guard, load_look_llm, load_look_prompt
from shopsteward.editing.live_look import live_look_error, live_look_open
```

Add a live-adapter builder:

```python
def _build_look_adapter(live_look: bool):
    """Live Claude adapter when --live-look is set and the gate is open;
    otherwise the offline fixture. Refuses if --live-look is set but gated off."""
    if not live_look:
        return _default_look_adapter(), False
    if not live_look_open():
        raise typer.BadParameter(live_look_error())
    llm = load_look_llm()
    import os
    adapter = OpenRouterLookAdapter(
        api_key=os.environ["OPENROUTER_API_KEY"],
        prompt_template=load_look_prompt(),
        pricing=llm.get("pricing"),
        temperature=float(llm.get("temperature", 0.7)),
    )
    return adapter, True
```

Add `--live-look` to the `run` command signature (after `batch_lock`):

```python
    live_look: Annotated[bool, typer.Option(help="Generate a described look via the live LLM (gated)")] = False,
```

And rework the `run` body to build the adapter + pass the gating config:

```python
    conn = connect(db_path())
    try:
        migrate(conn)
        adapter, is_live = _build_look_adapter(live_look)
        llm = load_look_llm()
        report = run_edit(
            conn, DEFAULT_USER_ID, Path(path), look,
            decoder=_default_decoder(), look_adapter=adapter,
            model=llm.get("model", model), knobs=load_correction_knobs(),
            regenerate=regenerate, overwrite=overwrite, batch_lock=batch_lock,
            guard_knobs=load_look_guard() if is_live else None,
            soft_cap_usd=llm.get("monthly_soft_cap_usd") if is_live else None,
            pricing=llm.get("pricing") if is_live else None,
            fallback_look=load_look_guard().get("fallback_look", "bright-and-true"),
            month_prefix=datetime.now(timezone.utc).strftime("%Y-%m"),
        )
        typer.echo(
            f"look={report.look} processed={report.processed} written={report.written} "
            f"skipped_existing={report.skipped_existing} failed={report.failed}"
        )
    finally:
        conn.close()
```

(Keep `_default_decoder`, `_default_look_adapter` as-is. Remove the now-unused `model` default only if it's no longer referenced — it is still referenced as the fallback in `llm.get("model", model)`, so keep the `model` option.)

- [ ] **Step 4: Run → 2 passed. Full editing suite green. Commit**

```bash
git add src/shopsteward/editing/cli.py tests/editing/test_cli_live_look.py
git commit -m "feat(editing): add `edit run --live-look` (gated live generation)"
```

---

## Task 8: `edit look preview` A/B command

**Files:**
- Modify: `src/shopsteward/editing/cli.py`
- Create: `src/shopsteward/editing/preview.py`
- Test: `tests/editing/test_preview.py`

Writes candidate + comparison-seed sidecars into two subfolders of `<sample_dir>/_preview/` for side-by-side Lightroom import. Correction is identical across both (isolates the look).

- [ ] **Step 1: Write the failing test**

`tests/editing/test_preview.py`:

```python
from pathlib import Path

import numpy as np

from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.editing.config import LOOKS_DIR, load_correction_knobs
from shopsteward.editing.preview import run_preview
from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

USER = 1


def test_preview_writes_candidate_and_seed(tmp_path: Path):
    raw = tmp_path / "A.CR3"
    raw.write_bytes(b"a")
    decoder = FakeRawDecoder({str(raw): DecodedImage(rgb=np.full((8, 8, 3), 0.3, np.float32))})
    conn = connect(":memory:")
    migrate(conn)
    out = run_preview(conn, USER, tmp_path, "bright-and-true", against="national-geographic",
                      decoder=decoder, look_adapter=FixtureLookAdapter(), model="fixture",
                      knobs=load_correction_knobs(), looks_dir=LOOKS_DIR)
    assert (tmp_path / "_preview" / "bright-and-true" / "A.xmp").exists()
    assert (tmp_path / "_preview" / "national-geographic" / "A.xmp").exists()
    assert out["candidate"] == "bright-and-true"
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `preview.py`**

`src/shopsteward/editing/preview.py`:

```python
"""A/B look preview: write a candidate look and a comparison seed as sidecars
into <sample_dir>/_preview/<look>/ subfolders (copied RAWs + sidecars) so the
operator can import both into Lightroom and compare. Correction is identical
across both, isolating the look. Non-destructive: writes only under _preview/."""

import shutil
import sqlite3
from pathlib import Path

from shopsteward.adapters.look.interface import LookAdapter
from shopsteward.editing import looks
from shopsteward.editing.analyze import analyze_raw
from shopsteward.editing.ingest import RAW_SUFFIXES
from shopsteward.editing.rawdecode import RawDecoder
from shopsteward.editing.xmp import compose, write_sidecar


def run_preview(
    conn: sqlite3.Connection, user_id: int, sample_dir: Path, look_arg: str, *,
    against: str, decoder: RawDecoder, look_adapter: LookAdapter, model: str,
    knobs: dict, looks_dir: Path, **resolve_kwargs,
) -> dict:
    looks.seed(conn, user_id, looks_dir)
    candidate = looks.resolve_look(conn, user_id, look_arg, look_adapter,
                                   model=model, regenerate=False, **resolve_kwargs)
    seed = looks.get_look(conn, user_id, against)

    raws = sorted(p for p in Path(sample_dir).iterdir()
                  if p.is_file() and p.suffix.lower() in RAW_SUFFIXES)
    preview_root = Path(sample_dir) / "_preview"
    for label, look in ((candidate.name, candidate), (seed.name, seed)):
        sub = preview_root / label
        sub.mkdir(parents=True, exist_ok=True)
        for rp in raws:
            correction = analyze_raw(decoder.decode(str(rp)), knobs)
            dest = sub / rp.name
            shutil.copy2(rp, dest)
            write_sidecar(dest, compose(correction, look), overwrite=True)
    return {"candidate": candidate.name, "against": seed.name,
            "frames": len(raws), "dir": str(preview_root)}
```

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Add the CLI command**

In `src/shopsteward/editing/cli.py`, add a `look` sub-app + `preview` command:

```python
look_app = typer.Typer(no_args_is_help=True, help="Look tools: A/B preview.")
edit_app.add_typer(look_app, name="look")


@look_app.command("preview")
def look_preview(
    sample_dir: Annotated[str, typer.Argument(help="Folder of a few sample RAWs")],
    look: Annotated[str, typer.Option(help="Candidate look name or description")],
    against: Annotated[str, typer.Option(help="Comparison seed look")] = "bright-and-true",
    live_look: Annotated[bool, typer.Option(help="Generate a described candidate via the live LLM")] = False,
) -> None:
    """Write candidate + comparison sidecars into <sample_dir>/_preview/ for LR compare."""
    from datetime import datetime, timezone

    from shopsteward.editing.config import LOOKS_DIR
    from shopsteward.editing.preview import run_preview

    conn = connect(db_path())
    try:
        migrate(conn)
        adapter, is_live = _build_look_adapter(live_look)
        llm = load_look_llm()
        out = run_preview(
            conn, DEFAULT_USER_ID, Path(sample_dir), look, against=against,
            decoder=_default_decoder(), look_adapter=adapter,
            model=llm.get("model", "fixture"), knobs=load_correction_knobs(), looks_dir=LOOKS_DIR,
            guard_knobs=load_look_guard() if is_live else None,
            soft_cap_usd=llm.get("monthly_soft_cap_usd") if is_live else None,
            pricing=llm.get("pricing") if is_live else None,
            fallback_look=load_look_guard().get("fallback_look", "bright-and-true"),
            month_prefix=datetime.now(timezone.utc).strftime("%Y-%m"),
        )
        typer.echo(f"preview: candidate={out['candidate']} vs {out['against']} "
                   f"— {out['frames']} frames in {out['dir']}")
    finally:
        conn.close()
```

- [ ] **Step 6: Full gate + commit**

Run: `uv run pytest -q` (green), `uv run ruff check src tests` (clean), `uv run lint-imports` (contracts kept — editing must NOT import pipeline), `uv run shopsteward edit --help` (shows `run` + `look`).

```bash
git add src/shopsteward/editing/cli.py src/shopsteward/editing/preview.py tests/editing/test_preview.py
git commit -m "feat(editing): add `edit look preview` A/B command"
```

---

## Task 9: Live smoke (operator, gated — not in CI)

**Not a code task.** With `SHOPSTEWARD_LIVE_LOOK=1` and `OPENROUTER_API_KEY` set, the operator runs `shopsteward edit look preview <few RAWs> --look "cinematic mexico" --live-look` to confirm: the Claude model returns a parseable profile (or that the prompt-guided fallback is needed — see Task 1), the guard passes/rejects sanely, and an `llm.call` cost event is recorded. Adjust `look_llm.model`/`pricing` or the guard knobs if needed. This validates the live path the mocked tests can't.

---

## Self-review (against the spec)
- Live gate (Task 2), Claude model config + pin (Task 1), `--live-look` wiring + refuse-when-closed (Task 7), cost ledger + hard soft-cap (Tasks 4–5), sanity guard retry→fallback (Tasks 3, 5), A/B preview (Task 8), fixtures-only tests + operator live smoke (Task 9). Editing-module-locality preserved (no pipeline imports; import-linter checked in Tasks 7–8).
- Placeholder scan: the only intentional deferral is the Claude model id/pricing, pinned in Task 1 via find-docs/claude-api and surfaced to the operator — not a code placeholder.
- Type consistency: `resolve_look` param names (`guard_knobs`, `soft_cap_usd`, `pricing`, `fallback_look`, `month_prefix`) match across Tasks 5–8; `LookVerdict`, `sanitize_look`, `append_llm_call`, `month_look_cost`, `_build_look_adapter`, `run_preview` used consistently.
