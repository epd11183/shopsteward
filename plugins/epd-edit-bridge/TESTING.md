# Manual test checklist — ShopSteward queue processor (v1.1)

The queue processor touches a live Lightroom catalog, so it is tested by
hand against canned job files. Budget ~15 minutes. Use a scratch catalog
if you have one; everything the processor does is undoable, but scratch is
scratch.

## 0. Setup

1. Pick (or create) a bridge folder, e.g. `C:\temp\bridge`.
2. Put two or three small JPEGs somewhere on disk (they stand in for RAWs;
   Lightroom imports them the same way). Note their paths.
3. Generate the canned jobs:

   ```
   python make_test_jobs.py --bridge-dir C:\temp\bridge
   ```

4. Open the generated files in `C:\temp\bridge\jobs\` and replace every
   `C:/EDIT_ME/...` `raw_path` (jobs 1, 2, 6) with your real image paths —
   **forward slashes**, as the Python dispatcher writes them.

## 1. Install / reload the plugin

- Lightroom Classic → File → Plug-in Manager → the EPD Edit Bridge plugin →
  **Reload Plug-in** (or Add, first time).
- Library → Plug-in Extras should now show three entries, including
  *EPD: Start/Stop ShopSteward Queue Processor*.

## 2. Authorization prompt

- Run the menu item. First run asks you to pick the bridge folder
  (choose `C:\temp\bridge`, the folder that *contains* `jobs/`), then shows
  the authorization dialog listing the jobs path and the four powers
  (import, apply, collections, export).
- **Click Cancel.** Expect: nothing happens — no files move, no task runs.
- Run the menu item again and click **Start processor**.

## 3. Expected outcome per canned job

Within a few sweeps (3 s apart) every file should leave `jobs/`:

| Job file | Lands in | Result file to eyeball | Catalog effect |
|---|---|---|---|
| `edit_test1_valid_*` | `jobs/done/` | `status=completed`, `applied=2`, `exported=["testev-0001.jpg","testev-0002.jpg"]`, `skipped=[]` | Both photos imported (if new), one "ShopSteward preset" history step each, collection **ShopSteward — test-valid** contains both; JPEGs in `bridge/test_exports/valid/` |
| `edit_test2_skip_*` | `jobs/done/` | `applied=1`, `skipped=[{"base_name":"GHOST","reason":"not_in_catalog"}]`, `exported=["skipev-0001.jpg"]` | Only the real photo touched |
| `edit_test3_malformed_*` | `jobs/failed/` | `status=failed`, `error.code="malformed"`, message mentions JSON parse error, `file_name` set | None |
| `edit_test4_wrongschema_*` | `jobs/failed/` | `error.code="malformed"`, message mentions schema | None |
| `edit_test5_nophotos_*` | `jobs/failed/` | `error.code="malformed"`, message "photos must be a non-empty array" | None |
| `edit_test6_unicode_*` | `jobs/done/` | `applied=1`, exported name `smith–wedding café-0001.jpg`, unicode intact | Collection **ShopSteward — tëst-únicode** |

Each result file is `<jobname>.result.json` beside the moved job file and
must contain `"schema": "shopsteward.editresult/1"`.

Also check: a per-job progress indicator titled *ShopSteward: test-family*
appears in the LrC progress area while a job runs.

## 4. Undo check

- Select one of the photos from job 1 in Develop.
- History panel: the apply shows as a single **ShopSteward preset** step.
- One Ctrl+Z (per photo) reverts the develop apply and nothing else.

## 5. Stop check

- Run the menu item again. Expect the message "will stop after the current
  sweep".
- Drop any new `.json` into `jobs/` — it must stay there (no processing).
- Restarting via the menu re-prompts for authorization (per-session).

## 6. Re-run safety (optional)

- Copy a processed job file from `jobs/done/` back into `jobs/` while the
  processor runs. It should process again cleanly: absolute develop values
  are idempotent, exports overwrite the previous JPEGs deterministically
  (same names, no collision suffixes), and the done/ copies are overwritten.
