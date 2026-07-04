# M2 Editing Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Spec detail lives in `docs/designs/2026-07-03-m2-editing-module.md` (approved 2026-07-03) — every task below names its design section. Branch: `m2/editing-module`. Reviewer pass per task group; PR at the end (first PR of milestone = operator review).

**Goal:** Standalone editing module — folder-pointed ingestion (hero/mass), RAW+JPEG pairing, preset families, EPD Edit Bridge queue processor with auto-import/apply/export, fake-bridge-tested end to end without Lightroom.

**Architecture:** design doc §1–§4. Event-sourced (new event types §2), one shared folder protocol (`core/folderproto.py`), resolved settings inlined in schema-v1 job files, pure-Lua helper modules + manual TESTING.md checklist for the Lightroom side.

**Tech stack:** existing (Python 3.12/Pydantic v2/Typer/FastAPI/SQLite, React+Vite) + `import-linter` (dev, approved).

### Task A: import-linter boundary contract (first commit of the PR)
- Add `import-linter>=2` to `[dependency-groups].dev`; `[tool.importlinter]` contract per design §6 (create `src/shopsteward/editing/__init__.py` so the contract has a target; add placeholder empty modules only as needed).
- CI: `uv run lint-imports` step in the lint-test job.
- Test: contract passes; a scratch verification that adding `from shopsteward.adapters.etsy import models` to editing fails `lint-imports` (do locally, don't commit the failure).
- Commit: `build: enforce editing-module boundary with import-linter from day one`

### Task B: core/folderproto.py
- Per design §3: `write_manifest(dir, name, payload, schema)` (tmp `.part` + `os.replace`), `read_manifests(dir, schema_prefix)` (skip `.part`, malformed → `quarantine/` + return error records), `complete(path, outcome: done|failed, result_payload)` helpers; layout constants.
- Tests (`tests/core/test_folderproto.py`): atomic write leaves no `.part` on success; malformed JSON quarantined not raised; done/failed moves; schema mismatch rejected; idempotent re-read.
- Commit: `feat(core): shared atomic folder-handoff protocol (folderproto)`

### Task C: editing models, preset families, projections
- `editing/models.py` (design §1), `editing/presets.py` (seed from `config/defaults/preset_families/*.json` → `presetfamily.seeded` events, last-write-wins projection; `get(name)` returns resolved settings dict), `editing/projections.py` (4 tables, drop/rebuild style, all with user_id).
- Create `config/defaults/preset_families/{neutral,wedding,race,brewery}.json` with placeholder develop-settings (real Lr keys: Contrast2012, Vibrance, etc.) and `config/defaults/editing.json` (`naming_template`, `event_output_root`, `jpeg_quality: 92`).
- Tests: seeding idempotent; unknown preset error lists available; projections rebuild.
- Commit: `feat(editing): preset families (DB-seeded) + editing projections`

### Task D: ingestion
- `editing/ingest.py` per design §2: scan folder, pair `.CR3`/`.JPG|.JPEG` case-insensitively by base name, chunked sha256(RAW) = photo_id, EXIF from JPEG via Pillow (capture time, camera, lens, ISO, dims — tolerate missing EXIF), events for ingested/duplicate/unpaired, `ingest.started`/`.completed`, IngestReport return.
- Tests: pairing incl. orphan both directions, dedupe across two runs, EXIF-less JPEG, empty folder.
- Commit: `feat(editing): folder-pointed ingestion with RAW+JPEG pairing`

### Task E: lightroom adapter + dispatch + outcomes
- `adapters/lightroom/{interface,bridge,fake}.py`, `editing/dispatch.py`, `editing/outcomes.py` per design §3. `SHOPSTEWARD_BRIDGE_DIR` in settings.py (default `data/bridge`). FakeBridge implements the full Lua consumer contract incl. `render_name` port and malformed-job quarantine.
- Tests: dispatch writes schema-v1 job with inlined settings + forward-slash paths; FakeBridge produces done/failed + results; outcomes scan idempotent by job_id; hero job has `export: null`.
- Commit: `feat(editing): lightroom bridge adapter, job dispatch, outcome ingestion`

### Task F: CLI + API + E2E
- Wire real `ingest` command (replaces stub; flags per design §5, confirm prompt, `--yes`), `edit` sub-app (presets list/show/seed, status), `editing/api.py` router mounted in `api.py`.
- `tests/editing/test_e2e_mass_mode.py` exactly per design §7 E2E paragraph.
- Commit: `feat(editing): CLI + API surface and mass-mode end-to-end test`

### Task G: Lua plugin (lua-impl agent, `plugins/epd-edit-bridge/` only)
- `JsonCodec.lua`, `JobFile.lua` (pure; validate/render_name/result-build), `QueueProcessor.lua` (design §4: authorize→loop→pcall process: find/addPhoto→withWriteAccessDo apply+collection→LrExportSession→rename→result→done/), `Info.lua` v1.1 menu toggle, `TESTING.md` manual checklist + committed Python fixture-generator script `plugins/epd-edit-bridge/make_test_jobs.py` (6 canned jobs per design §4).
- Commit: `feat(plugin): ShopSteward queue processor v1.1 (import, apply, export)`

### Task H: frontend Ingest page
- `frontend/src/pages/Ingest.tsx` + minimal routing from App: path input, mode toggle, preset dropdown (`GET /api/editing/preset-families`), submit → `POST /api/editing/ingest`, 3s-poll job table (`GET /api/editing/jobs`). Tailwind, no new libs.
- Commit: `feat(ui): ingest page with mode toggle and job status`

### Task I: wrap-up
- Reviewer pass over full branch; full suite + lint-imports + frontend build green; PR `M2: standalone editing module` (operator review).
