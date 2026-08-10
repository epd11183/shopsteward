# Winners-folder Shop-building Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Etsy **digital** listing drafts from a manual winners folder, with a gated **vision-for-copy** pass replacing the deleted scoring signals, and a one-command `shop build <folder>` orchestration.

**Architecture:** All in `pipeline/` (may import `adapters/vision`, `pipeline.tuning`, `pipeline.live_gate`, `pipeline.llm_ledger`). Reuses the existing landing → mockups → listings machinery, which is already landing-file-keyed and manual-drop-tolerant. No editing-module involvement. Fixtures by default; live vision/Etsy gated.

**Spec:** `docs/superpowers/specs/2026-08-10-winners-folder-shop-phase-a-design.md`

## Verified facts (from the current code)
- `VisionAdapter.score_commercial(jpeg_bytes: bytes, *, model: str) -> VisionResult`; `VisionResult.verdict` is a `VisionVerdict` (subject, strongest_room_style, one_risk, rationale) + `.usage` (VisionUsage w/ est_cost_usd). `FixtureVisionAdapter()` offline; `FakeVisionAdapter([...])` for tests.
- `proj_scores` + the `photo.scored` projection handler (`_fold_photo_scored`) are KEPT. It reads `payload["vision"]["rescore" or "triage"]["verdict"]` and `payload["scores"]`, writing subject/strongest_room_style/one_risk/rationale into `proj_scores` keyed by `photo_id`.
- `copy.generate_copy(conn, user_id, draft_id, landing_file_id, photo_id, images, adapter, cfg, *, live, soft_cap_usd)`; `copy._build_inputs` reads `proj_scores WHERE photo_id=?` only when photo_id is not None.
- `build_drafts` walks `proj_landing_files WHERE status='valid'`, tolerates `photo_id IS NULL`, and already uses the synthetic id form `file-<file_id[:12]>`.
- `tuning_profile.vision` (provider, model ids, `est_cost_per_mtok`, `monthly_soft_cap_usd`) is KEPT; `live_gate.live_vision_open(provider)` / `live_vision_error(provider)` KEPT; `llm_ledger.current_month_prefix()` / `monthly_spend(conn, user_id, month_prefix)` KEPT; `COMMERCIAL_PROMPT_PATH` KEPT. Only `pipeline/vision_factory.py` was deleted.

---

## Task 1: Restore the vision adapter builder

**Files:**
- Create: `src/shopsteward/pipeline/vision_factory.py` (restore from git)
- Test: `tests/pipeline/test_vision_factory.py` (restore from git)

- [ ] **Step 1: Restore both files from the pre-ripout commit**

```bash
git show 853769b:src/shopsteward/pipeline/vision_factory.py > src/shopsteward/pipeline/vision_factory.py
git show 853769b:tests/pipeline/test_vision_factory.py > tests/pipeline/test_vision_factory.py
```
(`853769b` is the merge commit of PR #21, immediately before the gating ripout PR #22.)

- [ ] **Step 2: Verify it still imports cleanly against current code**

Run: `uv run python -c "from shopsteward.pipeline.vision_factory import build_vision_adapter; print('ok')"`
Expected: `ok`. If it fails (e.g. a symbol it imported was removed), fix the import to match current `pipeline/config.py` + `pipeline/models.py` (both `COMMERCIAL_PROMPT_PATH` and `TuningProfile.vision` still exist, so it should import as-is).

- [ ] **Step 3: Run the restored test**

Run: `uv run pytest tests/pipeline/test_vision_factory.py -v`
Expected: pass. If a test referenced deleted scoring symbols, trim those cases to the fixture/live-build behavior only; keep the offline-returns-fixture and provider-selection cases.

- [ ] **Step 4: Commit**

```bash
git add src/shopsteward/pipeline/vision_factory.py tests/pipeline/test_vision_factory.py
git commit -m "feat(pipeline): restore vision adapter builder for copy-vision use"
```

---

## Task 2: Vision-for-copy step

**Files:**
- Create: `src/shopsteward/pipeline/listings/vision_copy.py`
- Test: `tests/pipeline/listings/test_vision_copy.py`

Runs a vision pass over each valid landing winner that lacks a real `photo_id`, emits a `photo.scored` event under the synthetic id `file-<file_id[:12]>`, ledgers the LLM cost, and refuses past the monthly soft cap. Copy-helper only — never rejects or edits a photo.

- [ ] **Step 1: Write the failing test**

`tests/pipeline/listings/test_vision_copy.py`:

```python
import pytest

from shopsteward.adapters.vision.fake import FakeVisionAdapter
from shopsteward.adapters.vision.interface import VisionResult, VisionUsage, VisionVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings.vision_copy import VisionCostCapError, run_vision_copy
from shopsteward.pipeline.projections import rebuild_pipeline

USER = 1


def _landing_winner(conn, file_id="abc123def456", path="/w/A.jpg"):
    append(conn, Event(user_id=USER, type="landing.file_observed", payload={
        "file_id": file_id, "path": path, "base_name": "A", "photo_id": None,
        "format": "JPEG", "width": 4000, "height": 3000, "color_space": "sRGB"}))
    rebuild_pipeline(conn)


def _conn(tmp_path):
    c = connect(":memory:")
    migrate(c)
    return c


def _verdict_result():
    return VisionResult(
        verdict=VisionVerdict(subject="trail runner", strongest_room_style="modern",
                              one_risk="busy background", rationale="dynamic motion"),
        usage=VisionUsage(model="m", est_cost_usd=0.01))


def test_scores_photoless_winner_under_synthetic_id(tmp_path, monkeypatch):
    c = _conn(tmp_path)
    _landing_winner(c)
    # Provide a fake image reader so no real file I/O is needed.
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    out = run_vision_copy(c, USER, adapter=FakeVisionAdapter([_verdict_result()]),
                          model="m", soft_cap_usd=5.0, month_prefix="2026-08")
    assert out["scored"] == 1
    rebuild_pipeline(c)
    row = c.execute("SELECT subject FROM proj_scores WHERE user_id=? AND photo_id=?",
                    (USER, "file-abc123def456")).fetchone()
    assert row["subject"] == "trail runner"


def test_idempotent_skip(tmp_path, monkeypatch):
    c = _conn(tmp_path)
    _landing_winner(c)
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    run_vision_copy(c, USER, adapter=FakeVisionAdapter([_verdict_result()]),
                    model="m", soft_cap_usd=5.0, month_prefix="2026-08")
    out = run_vision_copy(c, USER, adapter=FakeVisionAdapter([]),  # would raise if called
                          model="m", soft_cap_usd=5.0, month_prefix="2026-08")
    assert out["scored"] == 0 and out["skipped"] == 1


def test_soft_cap_refuses(tmp_path, monkeypatch):
    c = _conn(tmp_path)
    _landing_winner(c)
    append(c, Event(user_id=USER, type="llm.call", payload={"feature": "vision_copy",
                    "est_cost_usd": 5.0}))
    monkeypatch.setattr("shopsteward.pipeline.listings.vision_copy._read_bytes", lambda p: b"jpg")
    with pytest.raises(VisionCostCapError):
        run_vision_copy(c, USER, adapter=FakeVisionAdapter([_verdict_result()]),
                        model="m", soft_cap_usd=5.0, month_prefix="2026-08")
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Confirm the exact `photo.scored` payload shape**

Read `_fold_photo_scored` in `src/shopsteward/pipeline/projections.py` and the pre-ripout emitter `git show 853769b:src/shopsteward/pipeline/scoring.py` (search `photo.scored`). Match the payload so `proj_scores` populates: the handler reads `payload["vision"]["triage"|"rescore"]["verdict"]` and `payload["scores"]`. Emit at least `{"photo_id": <synthetic>, "scores": {}, "vision": {"triage": {"verdict": verdict.model_dump()}}}`.

- [ ] **Step 4: Implement**

`src/shopsteward/pipeline/listings/vision_copy.py`:

```python
"""Vision-for-copy: score each photo-less landing winner with the vision adapter
and emit a `photo.scored` event (synthetic id file-<file_id[:12]>) so listing
copy regains subject/style/risk signals. Copy helper only -- never rejects or
edits a photo. Gated + cost-capped; offline default is the fixture adapter."""

import sqlite3
from pathlib import Path

from shopsteward.adapters.vision.interface import VisionAdapter
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.llm_ledger import monthly_spend
from shopsteward.pipeline.projections import rebuild_pipeline


class VisionCostCapError(RuntimeError):
    """Raised when the month's vision-copy spend is already at/over the cap."""


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _synthetic_id(file_id: str) -> str:
    return f"file-{file_id[:12]}"


def _already_scored(conn: sqlite3.Connection, user_id: int) -> set[str]:
    done = set()
    for e in read_all(conn, "photo.scored"):
        if e.user_id == user_id:
            done.add(e.payload.get("photo_id"))
    return done


def run_vision_copy(
    conn: sqlite3.Connection, user_id: int, *, adapter: VisionAdapter, model: str,
    soft_cap_usd: float, month_prefix: str, regenerate: bool = False,
) -> dict:
    rebuild_pipeline(conn)
    rows = conn.execute(
        "SELECT file_id, path FROM proj_landing_files "
        "WHERE user_id=? AND status='valid' AND photo_id IS NULL ORDER BY file_id",
        (user_id,),
    ).fetchall()
    done = set() if regenerate else _already_scored(conn, user_id)

    scored = skipped = failed = 0
    for row in rows:
        sid = _synthetic_id(row["file_id"])
        if sid in done:
            skipped += 1
            continue
        if monthly_spend(conn, user_id, month_prefix) >= soft_cap_usd:
            raise VisionCostCapError(
                f"vision-copy spend for {month_prefix} is at the ${soft_cap_usd} cap; "
                "raise tuning_profile.vision.monthly_soft_cap_usd to continue"
            )
        try:
            result = adapter.score_commercial(_read_bytes(row["path"]), model=model)
        except Exception:  # noqa: BLE001 - per-photo vision failure is non-fatal
            failed += 1
            continue
        usage = result.usage
        if usage is not None:
            append(conn, Event(user_id=user_id, type="llm.call", payload={
                "feature": "vision_copy", "model": usage.model,
                "est_cost_usd": usage.est_cost_usd}))
        append(conn, Event(user_id=user_id, type="photo.scored", payload={
            "photo_id": sid, "scores": {},
            "vision": {"triage": {"verdict": result.verdict.model_dump()}}}))
        scored += 1

    rebuild_pipeline(conn)
    return {"scored": scored, "skipped": skipped, "failed": failed}
```

Note: confirm `monthly_spend` sums `llm.call` events' `est_cost_usd` (read it in Step 3); if it filters by a `feature` or lacks est handling, adapt the emitted payload to match what it reads. The cap-refusal test asserts a pre-seeded $5 `llm.call` triggers the raise.

- [ ] **Step 5: Run → 3 passed. Commit**

```bash
git add src/shopsteward/pipeline/listings/vision_copy.py tests/pipeline/listings/test_vision_copy.py
git commit -m "feat(listings): add gated vision-for-copy over landing winners"
```

---

## Task 3: Thread the synthetic photo_id into copy

**Files:**
- Modify: `src/shopsteward/pipeline/listings/drafts.py`
- Test: `tests/pipeline/listings/test_copy_manual_winner.py`

Ensure a photo-less winner's copy build receives `file-<id>` so `copy._build_inputs` finds the `proj_scores` row from Task 2.

- [ ] **Step 1: Read the two `generate_copy(...)` call sites** in `drafts.py` (≈ lines 180 and 236). Each passes a `photo_id` argument sourced from the landing row (`row["photo_id"]` / `existing["photo_id"]`).

- [ ] **Step 2: Write the failing test**

`tests/pipeline/listings/test_copy_manual_winner.py`: seed a photo-less `landing.file_observed` winner + a `photo.scored` row under `file-<id>` (subject="trail runner"), run `build_drafts` with the fixture copy adapter, and assert the resulting draft's copy/inputs reflect the subject signal (assert against `proj_listing_drafts` title/description or the CopyInputs the fixture echoes). Model it on the existing `tests/pipeline/listings/test_copy.py::_seed_score` helper for the `photo.scored` shape.

- [ ] **Step 3: Run → FAIL** (copy is generic because photo_id is None, so proj_scores isn't read).

- [ ] **Step 4: Implement**

At each `generate_copy(...)` call in `drafts.py`, replace the `photo_id` argument with the effective id:
```python
        effective_photo_id = row["photo_id"] or f"file-{landing_file_id[:12]}"
```
and pass `effective_photo_id` where `photo_id` was passed. (For the `_existing_draft` reuse branch at ~236, derive it the same way from the stored `landing_file_id`/`photo_id`.) Do not change behavior when a real `photo_id` exists.

- [ ] **Step 5: Run → pass. Also run `tests/pipeline/listings/ -q`** (existing copy/drafts tests stay green — a real photo_id path is unchanged; a photo-less winner with no score row still degrades to generic copy).

- [ ] **Step 6: Commit**

```bash
git add src/shopsteward/pipeline/listings/drafts.py tests/pipeline/listings/test_copy_manual_winner.py
git commit -m "feat(listings): feed vision-copy signals to manual-winner drafts"
```

---

## Task 4: `shop build` orchestration

**Files:**
- Create: `src/shopsteward/pipeline/shop.py` (orchestration)
- Modify: `src/shopsteward/cli.py` (register a `shop` sub-app) — or add to an existing CLI group; match how `pipeline_app`/`listings_app` are registered.
- Test: `tests/pipeline/test_shop_build.py`

One command: `scan_landing(folder)` → `run_vision_copy` (gated) → mockups on winners → `build_drafts`.

- [ ] **Step 1: Locate the mockups build entry point.** Grep the mockups module (`src/shopsteward/mockups/` and/or `pipeline`): find the function that composites staging-template mockups for landing winners and writes `proj_mockup_sets`/`proj_mockups` (the tables `drafts._completed_mockup_set` reads). Note its signature — the orchestration must call it between vision-copy and `build_drafts`, since `build_drafts` skips a winner with no completed mockup set. Record the exact call.

- [ ] **Step 2: Write the failing test**

`tests/pipeline/test_shop_build.py`: create a temp winners folder with one small valid JPEG (use PIL to write a >min-long-edge sRGB JPEG so `scan_landing` validates it), point `SHOPSTEWARD_DB` + landing at temp paths, run `run_shop_build(...)` with fixture vision + copy adapters (offline), and assert a row appears in `proj_listing_drafts`. Assert the returned summary counts (observed/scored/mockups/drafts).

- [ ] **Step 3: Implement `pipeline/shop.py`**

```python
"""Winners-folder orchestration: scan the landing folder, run vision-for-copy,
composite mockups, and build listing drafts -- the digital shop-build in one
call. All live steps are gated + default off."""

import sqlite3
from pathlib import Path

from shopsteward.pipeline import tuning
from shopsteward.pipeline.landing import scan_landing
from shopsteward.pipeline.listings.vision_copy import run_vision_copy
from shopsteward.pipeline.llm_ledger import current_month_prefix
from shopsteward.pipeline.vision_factory import build_vision_adapter
# + import the mockups build fn and build_drafts, per Steps 1 and the listings API


def run_shop_build(
    conn: sqlite3.Connection, user_id: int, folder: Path, *,
    live_vision: bool = False, live_copy: bool = False, live_etsy_write: bool = False,
    regenerate: bool = False,
) -> dict:
    profile = tuning.get_profile(conn, user_id)
    landing = scan_landing(conn, user_id, folder)

    vision_adapter = build_vision_adapter(profile, live=live_vision)
    vc = run_vision_copy(
        conn, user_id, adapter=vision_adapter, model=profile.vision.rescore_model,
        soft_cap_usd=profile.vision.monthly_soft_cap_usd,
        month_prefix=current_month_prefix(), regenerate=regenerate,
    )

    mockups = <call the mockups build fn from Step 1 over the winners>
    drafts = <call build_drafts(...) with the copy adapter (live_copy) and gating>

    return {"observed": landing.observed, "scored": vc["scored"],
            "mockups": mockups_summary, "drafts": drafts_summary}
```
Fill the mockups/drafts calls from Step 1 and the existing `listings` build API (`build_drafts` signature in `drafts.py`; build the copy adapter via `listings.copy.build_copy_adapter`). Respect the live gates: if a `--live-*` flag is set but its gate is closed, refuse up front (reuse `live_vision_open`/`live_copy_open`/`live_etsy_write_open` + their error messages).

- [ ] **Step 4: Add the CLI command**

Add a `shop` Typer sub-app with `build`:
```python
@shop_app.command("build")
def shop_build(
    folder: Annotated[str, typer.Argument(help="Folder of finished winner JPEGs")],
    live_vision: Annotated[bool, typer.Option(help="Live vision-for-copy")] = False,
    live_copy: Annotated[bool, typer.Option(help="Live listing copy")] = False,
    live_etsy_write: Annotated[bool, typer.Option(help="Push drafts to Etsy")] = False,
    regenerate: Annotated[bool, typer.Option(help="Re-run vision on already-scored winners")] = False,
) -> None:
    ...  # connect + migrate, run_shop_build(...), echo the summary, finally close
```
Register it where the other sub-apps are wired (match `src/shopsteward/cli.py`).

- [ ] **Step 5: Run the test → pass.** `uv run shopsteward shop build --help` shows the command.

- [ ] **Step 6: Commit**

```bash
git add src/shopsteward/pipeline/shop.py src/shopsteward/cli.py tests/pipeline/test_shop_build.py
git commit -m "feat(pipeline): add `shop build` winners orchestration"
```

---

## Task 5: Full gate + PR

- [ ] **Step 1: Full verification**

Run: `uv run pytest -q` (green), `uv run ruff check src tests` (clean), `uv run lint-imports` (contracts kept — the new pipeline code may import adapters.vision; editing must still not import pipeline), `uv run shopsteward shop build --help`.

- [ ] **Step 2: Operator live smoke (gated — not CI).** With `SHOPSTEWARD_LIVE_VISION=1` + `OPENROUTER_API_KEY`, drop a couple of finished JPEGs in a temp folder and run `shopsteward shop build <folder> --live-vision`; confirm `proj_scores` gets real signals, copy reflects them, drafts build, and an `llm.call` cost event lands. Etsy push stays fixture unless `--live-etsy-write` is separately approved.

- [ ] **Step 3: Push + PR** against `main`; note Phase B (Gelato) is the follow-on.

---

## Self-review (against the spec)
- Vision-for-copy gated + capped + idempotent (Task 2); adapter builder restored (Task 1); copy threading for photo-less winners (Task 3); one-command orchestration (Task 4); fixtures-only tests + operator live smoke (Task 5). Digital only; Gelato is Phase B. Import boundary: new code in pipeline/ (may use adapters.vision); verified in Task 5.
- Placeholder note: Task 4's mockups/drafts call lines are intentionally to-be-filled from a required in-situ discovery step (the mockups build entry point isn't pinned in this plan) — Step 1 makes that discovery explicit with a verification (drafts appear), rather than guessing a signature.
- Type/name consistency: `run_vision_copy`, `VisionCostCapError`, `_synthetic_id` (`file-<file_id[:12]>`), `run_shop_build` used consistently; synthetic-id convention matches `build_drafts`.
