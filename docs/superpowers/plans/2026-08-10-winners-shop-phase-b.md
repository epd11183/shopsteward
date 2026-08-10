# Winners-folder Shop-building Phase B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Wire the built POD slices 1–2 (variant selection + pricing + R2 print-file hosting) into `shop build`, so each winner also gets a costed physical draft with a hosted print file. Gelato create + Etsy push + enrich are Phase C.

**Architecture:** Orchestrate from top-level `src/shopsteward/shop.py` (already composes pipeline+mockups). POD build stays in `pipeline/listings/pod/`. Fixtures by default; R2 hosting gated by `--live-printfile`.

**Spec:** `docs/superpowers/specs/2026-08-10-winners-folder-shop-phase-b-design.md`

## Verified facts
- `build_pod_drafts(conn, user_id, *, photo_id=None, force=False, print_file_host=None) -> PodBuildReport`. Walks `proj_landing_files WHERE status='valid'`, tolerates `photo_id IS NULL`; emits `listingdraft.*` up to `print_file_hosted`; STOPS before provider create.
- `pod/factory.build_print_file_host(*, live: bool) -> PrintFileHost` (fake unless live).
- `pod/cli.py::build` shows the exact wiring: gate `live_printfile_open()` → `host = build_print_file_host(live=live_printfile)` → `build_pod_drafts(conn, user_id, print_file_host=host)`.
- `live_gate.live_printfile_open()` / `live_printfile_error()` exist (kept).
- `run_shop_build(conn, user_id, folder, *, live_vision, live_copy, live_etsy_write, regenerate)` currently ends at `build_drafts`; refuses up front on closed gates.
- `resolve_print_source_path` falls back to the winner JPEG; `catalog.py` drops sub-DPI variants.

---

## Task 1: Wire POD into `run_shop_build` + CLI + summary

**Files:**
- Modify: `src/shopsteward/shop.py`
- Modify: `src/shopsteward/cli.py` (`shop build` gains `--live-printfile`)
- Test: `tests/pipeline/test_shop_build_pod.py`

- [ ] **Step 1: Read** `src/shopsteward/shop.py::run_shop_build` (full), `pod/cli.py::build` (wiring), and `pod/models.py::PodBuildReport` (field names for the summary). Read the existing `tests/pipeline/test_shop_build.py` for the fixture-winner setup (PIL JPEG, landing path, offline adapters) to mirror it.

- [ ] **Step 2: Write the failing test**

`tests/pipeline/test_shop_build_pod.py`: mirror `test_shop_build.py`'s setup (temp winners folder with a valid PIL-written sRGB JPEG large enough to keep ≥1 POD variant per `pod.json` min_dpi; point `SHOPSTEWARD_DB` + landing at temp paths). Run `run_shop_build(conn, USER, folder)` fully offline. Assert:
  - a `listingdraft.print_file_hosted` event exists (POD ran through hosting), OR a POD draft row appears in the POD projection (read `pod/projections.py` for the table/view name);
  - the returned summary includes physical counts (e.g. `pod_drafts >= 1` / kept-variant count > 0).
Add a second test: `--live-printfile` path refuses when the R2 gate is closed — call `run_shop_build(..., live_printfile=True)` with the gate env unset and assert it raises `LiveGateClosedError` and that `proj_landing_files` was never created (refused before scan).

Run → FAIL.

- [ ] **Step 3: Implement in `shop.py`**

Add imports:
```python
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
from shopsteward.pipeline.listings.pod.factory import build_print_file_host
from shopsteward.pipeline.live_gate import live_printfile_error, live_printfile_open
```
Add `live_printfile: bool = False` to `run_shop_build`'s keyword-only params. Add to the up-front refusal set (alongside the existing vision/copy/etsy checks):
```python
    if live_printfile and not live_printfile_open():
        raise LiveGateClosedError(live_printfile_error())
```
After the `drafts = build_drafts(...)` call, add:
```python
    host = build_print_file_host(live=live_printfile)
    pod = build_pod_drafts(conn, user_id, print_file_host=host)
```
Extend the returned summary dict with physical counts from `pod` (use the actual `PodBuildReport` field names read in Step 1, e.g. `pod_drafts`, `pod_variants_kept`, `pod_variants_dropped`).

- [ ] **Step 4: Implement the CLI flag in `cli.py`**

Add `--live-printfile` to the `shop build` command (default False), thread it into `run_shop_build(..., live_printfile=live_printfile)`, and include the physical counts in the echoed summary line.

- [ ] **Step 5: Run** `uv run pytest tests/pipeline/test_shop_build_pod.py tests/pipeline/test_shop_build.py -v` → pass (Phase A shop-build test stays green). `uv run ruff check src tests`.

- [ ] **Step 6: Commit**

```bash
git add src/shopsteward/shop.py src/shopsteward/cli.py tests/pipeline/test_shop_build_pod.py
git commit -m "feat(shop): build costed physical POD drafts in shop build (print-file hosted)"
```

---

## Task 2: Print-source / DPI coverage for manual winners

**Files:**
- Test: `tests/pipeline/listings/pod/test_pod_winner_source.py`

Confirm (no product-code change expected) that a manually-dropped JPEG winner drives POD correctly.

- [ ] **Step 1: Write the test**

`tests/pipeline/listings/pod/test_pod_winner_source.py`: seed a photo-less `landing.file_observed` winner (a large valid JPEG dimension in the event, e.g. 6000×4000) + rebuild; run `build_pod_drafts(conn, USER, print_file_host=<fake>)`; assert ≥1 variant kept and a `print_file_hosted` event for it. Second case: a tiny winner (e.g. 800×600) → the large variants are dropped (`listingdraft.pod_skipped` or dropped-variant records) with no exception, and the digital path is unaffected. (Read `pod/models.py` + `pod/build.py` for the exact event/return shapes to assert against; reuse any existing pod test fixtures/helpers.)

- [ ] **Step 2: Run → pass** (expected to pass against current code; if it reveals a real gap for the JPEG-only winner, STOP and report rather than changing pricing/catalog logic unprompted).

- [ ] **Step 3: Commit**

```bash
git add tests/pipeline/listings/pod/test_pod_winner_source.py
git commit -m "test(pod): winner JPEG as print source + sub-DPI variant drop"
```

---

## Task 3: Full gate + PR

- [ ] **Step 1:** `uv run pytest -q` (green), `uv run ruff check src tests` (clean), `uv run lint-imports` (3 contracts kept), `uv run shopsteward shop build --help` (shows `--live-printfile`).
- [ ] **Step 2: Operator R2 smoke (gated — not CI).** With R2 creds + `SHOPSTEWARD_LIVE_PRINTFILE=1`, run `shopsteward shop build <a few winners> --live-printfile` and confirm print files land in the real bucket and drafts reach `print_file_hosted`. No Gelato/Etsy calls occur in Phase B.
- [ ] **Step 3: Push + PR** against `main`; note Phase C (Gelato create + Etsy push + enrich, incl. a live Gelato adapter) is the remaining follow-on.

---

## Self-review (against the spec)
- Wires built POD slices 1–2 into `shop build` (Task 1); `--live-printfile` gate + refusal (Task 1); physical counts in summary (Task 1); winner-JPEG print source + DPI drop coverage (Task 2); full gate + operator R2 smoke (Task 3). Slices 3–4 explicitly deferred to Phase C. Import boundary: shop.py (top level) composes pod; pipeline↛mockups unaffected; verified in Task 3.
- Placeholder note: exact `PodBuildReport` field names and POD projection table/event names are read in Task 1 Step 1 / Task 2 Step 1 rather than guessed here — each has a concrete assertion target.
