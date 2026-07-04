# M2 Design — Standalone Editing Module

*Architect output, approved by operator 2026-07-03 (auto-import: YES; naming
vars: `{event}/{date}/{seq}/{base}` frozen for schema v1; output folder:
`--out` flag with `editing.json` `event_output_root/<event>/` default).*

## 1. Module / file map

```
src/shopsteward/core/folderproto.py      ONE shared atomic-handoff utility: tmp+os.replace writes,
                                         schema-versioned JSON manifests, done//failed/ outcomes,
                                         quarantine/. Used by editing→Lua jobs folder now, by the
                                         M3 landing-folder watcher later. Protocol spec lives in its
                                         module docstring (mirrored as a comment block in QueueProcessor.lua).
src/shopsteward/editing/__init__.py
src/shopsteward/editing/models.py        Pydantic v2: PhotoPair, IngestReport, EditJobSpec, EditResult, PresetFamily.
src/shopsteward/editing/ingest.py        Folder scan, RAW+JPEG pairing by base name, chunked sha256 of the
                                         .CR3, EXIF from paired JPEG via Pillow, event emission, dedupe.
src/shopsteward/editing/presets.py       Preset-family store: seed from config/defaults/preset_families/*.json
                                         into DB (via events), list/get resolved develop-settings dicts.
src/shopsteward/editing/dispatch.py      Build EditJobSpec (settings inlined), write job file via folderproto
                                         through the lightroom adapter; emits editjob.dispatched.
src/shopsteward/editing/outcomes.py      Scan bridge done//failed/ result files → editjob.completed/failed
                                         events (idempotent by job_id).
src/shopsteward/editing/projections.py   proj_photos, proj_ingest_jobs, proj_edit_jobs, proj_preset_families;
                                         rebuild() same drop/rebuild style as core/projections.py.
src/shopsteward/editing/cli.py           Typer sub-app: the `shopsteward edit` surface.
src/shopsteward/editing/api.py           APIRouter mounted by top-level api.py (top-level importing editing is
                                         legal; the boundary is one-directional).
src/shopsteward/adapters/lightroom/interface.py   LightroomBridge Protocol: dispatch(job), poll_results().
src/shopsteward/adapters/lightroom/bridge.py      Folder implementation on core.folderproto (default root
                                                  data/bridge/, path in settings.py: SHOPSTEWARD_BRIDGE_DIR).
src/shopsteward/adapters/lightroom/fake.py        FakeBridge: Python reimplementation of the Lua consumer
                                                  contract for tests.
plugins/epd-edit-bridge/JsonCodec.lua    JSON encode (lifted from ExportSettings.lua) + minimal decoder. Pure.
plugins/epd-edit-bridge/JobFile.lua      Pure functions: validate job table vs schema v1, render_name(template,ctx),
                                         build result table. No Lr* imports.
plugins/epd-edit-bridge/QueueProcessor.lua  Background poll loop, authorization prompt, import+apply+collection+export.
plugins/epd-edit-bridge/Info.lua         + menu item "EPD: Start/Stop ShopSteward Queue Processor"; version 1.1.
config/defaults/preset_families/         neutral.json, wedding.json, race.json, brewery.json (placeholder dicts,
                                         keys = exact Lr develop-setting names).
config/defaults/editing.json             default naming template, event_output_root, jpeg quality.
frontend/src/pages/Ingest.tsx            folder path input + mode toggle + preset picker + job status table.
pyproject.toml                           + import-linter dev dep, [tool.importlinter], lint-imports in CI.
```

## 2. Events + projections

| Event | Payload |
|---|---|
| `ingest.started` | `{ingest_job_id, path, mode, preset_family?, event_name?, output_folder?}` |
| `photo.ingested` | `{photo_id, ingest_job_id, raw_path, jpeg_path, raw_sha256, exif:{...}, mode, status}` — status `awaiting_scoring` (hero) or `queued_for_edit` (mass). `photo_id` = raw sha256 (content-addressed). |
| `photo.duplicate_skipped` | `{ingest_job_id, raw_sha256, raw_path, existing_photo_id}` |
| `photo.unpaired` | `{ingest_job_id, path, reason: missing_jpeg\|missing_raw}` |
| `ingest.completed` | `{ingest_job_id, paired, duplicates, unpaired}` |
| `presetfamily.seeded` / `.updated` | `{name, settings:{...}, source: defaults\|bridge_export}` |
| `editjob.dispatched` | `{edit_job_id, ingest_job_id, photo_ids, preset_family, mode, collection, job_file, export:{output_folder, naming_template, event}}` |
| `editjob.completed` | `{edit_job_id, applied, skipped:[{base_name, reason}], exported:[names], finished_at}` |
| `editjob.failed` | `{edit_job_id?, file_name, error:{code, message}}` |

Projections: **proj_photos** (per-user photo_id, paths, sha256, mode, status
`awaiting_scoring | queued_for_edit | editing | edited | edit_failed`,
exif_json), **proj_ingest_jobs**, **proj_edit_jobs**, **proj_preset_families**
(last-write-wins by name). M3 scoring consumes `awaiting_scoring`.

## 3. Job-file protocol (schema-versioned)

Bridge layout: `bridge/jobs/` (Python writes), `bridge/jobs/done/`,
`bridge/jobs/failed/`, `bridge/quarantine/`. Writers write `<name>.part` then
rename (`os.replace` Python / `os.rename` Lua; job-id-unique names avoid dest
collisions). Consumers ignore `*.part`.

`jobs/edit_<uuid>.json` (Python → Lua):

```json
{ "schema": "shopsteward.editjob/1", "job_id": "…", "user_id": 1, "mode": "mass",
  "created_at": "…Z", "preset_family": "wedding",
  "develop_settings": { "Contrast2012": 18, "Vibrance": 24 },
  "photos": [ { "base_name": "IMG_1234", "raw_path": "C:/…/IMG_1234.CR3" } ],
  "collection": "ShopSteward — smith-wedding",
  "import_missing": true,
  "export": { "output_folder": "C:/…/gallery", "naming_template": "{event}-{seq:04}",
              "event": "smith-wedding", "jpeg_quality": 92, "color_space": "sRGB" } }
```

Resolved develop settings are **inlined** — Lua never reads DB/preset files
(no version skew). Forward-slash paths. `export` null for hero jobs (M3).

Lua outcome: move job file to `done/` (or `failed/`) + write
`edit_<uuid>.result.json` beside it (tmp+rename):
`{ "schema": "shopsteward.editresult/1", "job_id", "status": "completed|failed",
"applied", "skipped": […], "exported": […], "error": null|{code,message},
"finished_at" }`. Malformed job → `failed/` with result keyed by filename;
never a crash. Python observes by scanning `done/`+`failed/` on demand (no
daemon in M2); event append idempotent by job_id.

## 4. Lua queue processor

- **Start/authorize:** menu toggle; `LrDialogs.confirm` on start names the
  jobs folder and asks session authority to import photos, apply presets,
  create collections, and export. Declined → task never starts. Per-session
  only.
- **Loop:** `LrTasks.startAsyncTask`; sweep `jobs/*.json` (skip `*.part`);
  `pcall(JsonCodec.decode)` + `JobFile.validate`; malformed ⇒ `failed/` +
  result; valid ⇒ process; `LrTasks.sleep(3)`; `LrProgressScope` per job.
- **Process (one `pcall` per job):** `catalog:findPhotoByPath`; missing +
  `import_missing` ⇒ `catalog:addPhoto` (operator-approved 2026-07-03);
  still missing ⇒ `skipped[]` reason `not_in_catalog`. One
  `catalog:withWriteAccessDo("ShopSteward: apply <family>")`:
  `applyDevelopSettings` (one undoable history step per photo) + collection
  add. Mass only: `LrExportSession` → sRGB JPEG to `output_folder` under
  original names, then post-export rename via pure
  `JobFile.render_name(template, {event, date, seq, base})`. Write result,
  move to `done/`.
- **Failure:** pcall error ⇒ `failed/` + result `{code, message}`; loop
  continues. Crash mid-job re-runs on restart — safe: absolute develop
  values are idempotent; exports overwrite deterministically.
- **Testability:** JsonCodec/JobFile pure; committed Python fixture
  generator produces 6 canned jobs; expected outcomes in
  `plugins/epd-edit-bridge/TESTING.md` manual checklist.

## 5. CLI, API, UI

- `shopsteward ingest <path> --mode {hero,mass} [--preset FAM] [--event NAME] [--out DIR] [--yes]`
  — hero: pair/hash/record (`awaiting_scoring`). Mass: same + dispatch;
  `--preset` omitted ⇒ list + prompt; confirm "Apply 'X' to N photos → out?"
  unless `--yes`. `--out` defaults to `editing.json` `event_output_root/<event>/`.
- `shopsteward edit presets list|show <name>|seed`; `shopsteward edit status`.
- API: `POST /api/editing/ingest`, `GET /api/editing/preset-families`,
  `GET /api/editing/jobs` (scans outcomes first).
- UI: one page — path input, hero/mass toggle, preset dropdown (mass only),
  submit, polling job table.

## 6. import-linter contract

```toml
[tool.importlinter]
root_package = "shopsteward"

[[tool.importlinter.contracts]]
name = "editing module is standalone"
type = "forbidden"
source_modules = ["shopsteward.editing"]
forbidden_modules = [
  "shopsteward.adapters.etsy", "shopsteward.adapters.printful",
  "shopsteward.adapters.gelato", "shopsteward.adapters.instagram",
  "shopsteward.pipeline",
]
```
CI: `uv run lint-imports` beside pytest/ruff. Lands in the first M2 PR.

## 7. Risks

1. RAWs not in catalog → auto-import + per-photo skip reporting.
2. Hand-rolled Lua JSON decoder → flat schema, pure module, shared fixtures
   validated by Python round-trip tests.
3. Crash/restart double-apply → idempotent absolute settings; documented.
4. Windows rename semantics → `os.replace` (Python), unique names (Lua).
5. Boundary erosion → CI-blocking contract from PR 1; folderproto in core.

**E2E without Lightroom** (`tests/editing/test_e2e_mass_mode.py`): tmp DB +
tmp bridge + 3 Pillow JPEGs + 3 stand-in `.CR3` + orphan + duplicate →
ingest mass → job file asserted → FakeBridge.consume_all() (+1 malformed) →
outcomes scan → events + proj_photos `edited` + template-named exports.

## 8. Rejected alternatives

- Localhost HTTP bridge server — fragile LrC sockets, server lifecycle;
  folder protocol reused for M3. Boring wins.
- Lua reads presets from DB/shared file — inlining removes skew and keeps
  the plugin dumb.
- LR export-rename tokens — can't express arbitrary templates; pure rename
  function is desk-checkable.
- Mutable CRUD tables — violates event-sourcing; projections are free.
