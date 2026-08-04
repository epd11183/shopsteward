# M2b Design — Per-Image Corrections Proposer (WB, Exposure/Tone, Horizon Leveling)

*Status: DRAFT, pending operator review (PRD §8.2). Architect output 2026-08-03.
Read-only design; not implemented. Scope is the editing module
(`src/shopsteward/editing/`), which is M2/M3 territory — see §14 for the
milestone-placement recommendation. New PRD §13 decision candidates are numbered
50–56 (§21); 42–49 are taken by M5b. Nothing here is approved: §15 is the
operator-decision list and several slices are blocked on it.*

Extends house patterns exactly: append-only `core.events`, drop-and-rebuild
module-local projections with a `rebuild_*()` entrypoint, config seeded from
`config/defaults/*.json` via events with a `config_hash`, on-demand runs (no
daemons), Typer sub-apps, deterministic OpenCV/Pillow computation with no
adapter (the `pipeline/scorers/technical.py` precedent). Consumes `proj_photos`
(the paired JPEG, M2) and produces per-file develop settings inlined into the
existing edit-job file. No new dependency, no network, no AI.

## 1. Two findings that constrain the design

**(a) The per-file transport does not exist on the automated path.**
`plugins/epd-edit-bridge/ApplySettings.lua` supports `global` + `byFile`, but
that is the *manual, operator-picks-a-file* command. ShopSteward's automated
path is `QueueProcessor.lua`, which applies one flat table to every photo:

```lua
photo:applyDevelopSettings(job.develop_settings, 'ShopSteward preset')
```

and `JobFile.validate` only checks `type(tbl.develop_settings) == 'table'`.
There is no per-file channel in `shopsteward.editjob/1`, and
`EditJobSpec.develop_settings` is a single `dict[str, float|int|str]`. Building
the proposer therefore **requires a job-schema change and a plugin change** —
this is not "the transport exists". §10 specifies it.

**(b) Lightroom `Temperature` is absolute Kelvin on a camera-specific Adobe
scale.** For a RAW file the develop value is e.g. `4800`, not an offset. The
paired JPEG has *already* been white-balanced by the camera's as-shot setting,
so measuring it yields the **residual error**, not the illuminant. Recovering
the as-shot baseline in Adobe's scale would need the RAW's WB multipliers *and*
the camera profile's forward matrices — there is no published mapping, and
Canon's MakerNote Kelvin is not Adobe's Kelvin. **The only trustworthy source of
the baseline is Lightroom itself.** Hence WB requires a read-back round trip
(§8) and is deliberately the last slice. Exposure and leveling do not: their
baselines are known in Python (preset-family value; no crop).

## 2. Proposal (one paragraph)

A deterministic CV module, `src/shopsteward/editing/corrections.py`, measures
the paired JPEG and emits **bounded, clamped, per-file absolute develop
settings** that are merged into the same edit job that applies the preset
family — so they arrive in Lightroom as the *starting point* of the Gate 2 pass,
never as something to approve. Every axis can **abstain**; a wild estimate
abstains rather than clamping to the maximum. All bounds, gains, deadbands and
per-mode overrides live in `config/defaults/corrections.json`, DB-seeded via an
event. What the operator ends up with after finishing is captured by the same
read-back mechanism, giving a proposed-vs-kept ledger — captured now, learned
from never (v1).

## 3. Dataflow

```
proj_photos.jpeg_path ──> corrections.measure()  (cv2/numpy, offline, no network)
      │                        │
      │                        ├─ exposure: linearise sRGB -> percentile -> ΔEV
      │                        ├─ leveling: Canny -> HoughLinesP -> median angle
      │                        └─ wb:       robust gray-world on mid-luminance band -> Δ ratios
      │                                     (needs a baseline: see the report phase)
      v
config/defaults/corrections.json (DB-seeded, config_hash)
      │  bounds, deadbands, gains, abstain_factor, per-mode overrides
      v
  propose()  -> per-axis {raw, delta, absolute_value, clamped} | abstained{axis: reason}
      │        corrections.proposed  (ONE event, includes abstentions + measurements)
      v
  dispatch_edit_job(..., by_file={base_name: {Exposure2012: .., CropAngle: ..}})
      │        editjob.dispatched (existing event, + corrections_config_hash)
      v
  jobs/edit_<uuid>.json  schema shopsteward.editjob/2   <-- NEW: develop_settings_by_file
      v
  QueueProcessor.lua: merge(global, by_file[base]) -> ONE applyDevelopSettings per photo
      │                                                 = ONE undoable history step (unchanged)
      v
  Gate 2 (hero) / optional review (mass): the operator edits normally. Export = approval.
      v
  [feedback, slice 4] report job -> photo.develop_observed{phase:"post_finish"}
                                 -> proj_corrections.kept_json (proposed vs kept)
```

No operator-facing surface is added anywhere in this diagram. That is the point.

## 4. Deterministic CV, not a vision model

**v1 is deterministic CV. No seam is built for a model-based proposer.**

- **Testability.** A synthetic image with a known +1.5° tilt or a known ×1.2 red
  gain has a *known correct answer*; a vision model has none you can assert
  offline. Every guardrail in this repo ("never call live external APIs in
  tests") pushes the same way.
- **Cost/scale.** Mass mode is 2,000 frames per event. Per-frame vision calls
  blow the $10/mo soft cap (decision 36) in one wedding; CV is free.
- **The editing module must run with zero credentials.** PRD §11 makes that a
  success metric, and the import-linter contract forbids
  `shopsteward.adapters.vision` to `shopsteward.editing`. A model-based proposer
  inside editing would require amending the boundary contract — the exact
  erosion PRD §12 names as a risk.
- **No interface with one implementation.** `pipeline/scorers/technical.py` is
  the precedent: deterministic OpenCV, plain module, no adapter Protocol.
  Corrections is the same class of thing. If a model-based proposer is ever
  authorized it belongs in `pipeline/` behind the *existing* `VisionAdapter`,
  proposing into the same `corrections.proposed` event — that seam gets built
  the day it has a second implementation, not before.

Deliberate simplification with a known ceiling: gray-world WB fails on
strongly monochromatic scenes (a red-gel dance floor, a green forest canopy),
and Hough leveling fails where the dominant long lines are not the horizon.
Both are handled by the abstain path (§6), not by a smarter algorithm.

## 5. The "AI never touches the photograph" reading

**v1 does not exercise this at all** — nothing here is a model. The reading is
proposed so a future model-based proposer is not re-litigated, and it is an
**operator decision (§15.2), not an architect's assumption.**

*Proposed reading (PRD §13 candidate 50):* the rule forbids any model that
**emits or modifies pixels of a photograph**. A model that emits **numeric
develop parameters** interpreted by Lightroom's own deterministic raw engine,
recorded as a reversible history step, is not covered — it is the same class as
the vision *score* that already routes photos (§5.1), and it follows decision 34,
which already held that the rule "targets generative edits and the sold file".

*Still forbidden under this reading, explicitly:* generative edit, inpaint/fill,
sky replacement, model upscaling, model denoise, model-generated masks or
subject selections driving local adjustments, any model output that becomes part
of the delivered file's pixel data, and any model-proposed crop that changes
composition (leveling is not composition). If the operator rejects this reading,
the design is unaffected in v1 — only the future seam closes.

## 6. Bounds, clamps, and the abstain path

Wrong corrections are the real risk, so the bounding rule is asymmetric:

| Stage | Rule |
|---|---|
| **Deadband** | `|raw| < min_*` → delta 0. Do not touch what is already right. |
| **Clamp** | `min_* ≤ |raw| ≤ max_*` → apply, `clamped=true` if truncated to `max_*`. |
| **Abstain** | `|raw| > max_* × abstain_factor` (default 2.0) → **abstain, apply nothing.** An estimate that far out is far more likely to be a measurement failure than a genuinely large correction. Clamping it would silently apply the maximum — the exact "bad correction" failure mode. |
| **Confidence abstain** | leveling: fewer than `min_lines` supporting lines, or inter-quartile angle spread > `angle_tolerance_deg`. WB: mid-luminance band covers < `min_band_px_pct` of the frame. |
| **Scene guard** | Never propose a *positive* exposure delta when measured highlight-clip % already exceeds `clip_highlight_pct_max`; never propose negative exposure when shadow-clip % exceeds `clip_shadow_pct_max`. Reuses the two statistics `pipeline/scorers/technical.py` already computes — same formulas, recomputed locally (editing must not import pipeline). |
| **Unreadable** | `cv2.imread` returns None → abstain all axes, reason `unreadable`. |

Abstentions are per-axis: a frame can get an exposure correction and abstain on
leveling. **What the operator sees when it abstained: nothing.** The photo
arrives in Lightroom exactly as it does today. The record lives in
`corrections.proposed.abstained` and in `shopsteward edit status` counts. This is
deliberate — surfacing abstentions in Gate 1 or Gate 2 would create the fourth
touchpoint CLAUDE.md forbids.

**Where the bounds live:** `config/defaults/corrections.json`, DB-seeded via
`correctionsconfig.seeded`, never in Python. The gain constants (`temp_k_per_log2`,
`tint_per_log2`) are hand-set and **uncalibrated**; they are the tuning knob the
physical world requires, and `shopsteward edit corrections preview <path>`
(slice 1, dry-run, no writes, no dispatch) exists so the operator calibrates them
against real files locally.

```json
{ "schema": "shopsteward.corrections/1", "name": "default",
  "enabled": false,
  "analysis_long_edge_px": 1600,
  "exposure": { "enabled": true, "target_percentile": 99.5, "target_level": 0.90,
                "min_ev": 0.15, "max_ev": 0.50, "abstain_factor": 2.0,
                "clip_highlight_pct_max": 1.0, "clip_shadow_pct_max": 2.0 },
  "white_balance": { "enabled": true,
                     "lum_band": [0.20, 0.98], "min_band_px_pct": 5.0,
                     "temp_k_per_log2": 900, "tint_per_log2": 22,
                     "min_temp_delta_k": 75, "max_temp_delta_k": 400,
                     "min_tint_delta": 2, "max_tint_delta": 8,
                     "abstain_factor": 2.0 },
  "leveling": { "enabled": true,
                "canny": [60, 180], "min_line_frac": 0.25, "max_candidate_deg": 8.0,
                "min_lines": 3, "angle_tolerance_deg": 0.8,
                "min_rotation_deg": 0.3, "max_rotation_deg": 3.0,
                "abstain_factor": 2.0, "write_crop_rect": true },
  "modes": { "hero": {},
             "mass": { "exposure": {"max_ev": 0.35},
                       "white_balance": {"max_temp_delta_k": 300, "max_tint_delta": 6},
                       "leveling": {"enabled": false} } } }
```

`config_hash()` = sha256 of canonical compact JSON (M4/M5a precedent).
`enabled: false` ships as the default — see §15.5.

## 7. Measurement math (all offline, all in `corrections.py`)

- **Exposure (darktable-style).** Decode the paired JPEG, downscale to
  `analysis_long_edge_px`, **linearise the sRGB transfer function** (computing
  the ratio on gamma-encoded values is wrong by roughly a factor of two — this is
  the easy bug), take the `target_percentile` of linear luminance,
  `ΔEV = log2(target_level / measured)`. Absolute value =
  `preset_family.Exposure2012 (default 0) + ΔEV`, clamped. Baseline is known in
  Python; **no round trip.**
- **Leveling.** Grayscale → `cv2.Canny` → `cv2.HoughLinesP`; keep lines longer
  than `min_line_frac × width` whose angle is within `max_candidate_deg` of
  horizontal; length-weighted median angle. Verticals are not used (YAGNI —
  add when a real frame needs it). `CropAngle` = −median (sign convention
  **unverified**, resolved by the §9 spike). If `write_crop_rect`, also emit the
  largest aspect-preserving inscribed rectangle as normalized
  `CropTop/Left/Bottom/Right`; the closed form loses ≈6% of frame area at 3° on a
  3:2 frame, which is a second reason `max_rotation_deg` is small.
- **White balance.** Linearise, keep pixels whose luminance is inside `lum_band`
  (excludes speculars and shadows, which poison gray-world), robust means R̄/Ḡ/B̄.
  `Δ_temp_K = temp_k_per_log2 × log2(B̄/R̄)` (image too blue ⇒ warm it ⇒ raise
  Kelvin); `Δ_tint = tint_per_log2 × log2(Ḡ/√(R̄·B̄))` (too green ⇒ push Tint
  positive toward magenta). Absolute value = **reported baseline + Δ**, and
  `WhiteBalance = "Custom"` must accompany it (documented in `ApplySettings.lua`'s
  own header). Requires §8.

## 8. The WB baseline read-back (report phase)

A second job *kind* on the existing bridge, not a second bridge:

1. `dispatch_report_job(photo_ids)` → `jobs/edit_<uuid>.json` with
   `job_kind:"report"`, `develop_settings:{}`, `import_missing:true`.
2. `QueueProcessor.lua`: resolve/import photos, `photo:getDevelopSettings()`,
   emit the curated key set into the result — **no `applyDevelopSettings`, no
   collection, no export.** Read-only apart from the import already authorized
   by the session prompt.
3. `outcomes.scan_outcomes` folds the result into
   `photo.develop_observed{phase:"baseline"}`.
4. `corrections.advance(conn, user_id, bridge)` — called from the *same* places
   `scan_outcomes` is already called (`shopsteward edit status`,
   `GET /api/editing/jobs`, post-ingest) — finds photos with a baseline and no
   apply job, proposes, and dispatches the real apply job. On-demand, no daemon.

The same mechanism, run *after* Gate 2, is the feedback capture (§11). One
mechanism, two phases — that is why the round trip is worth its complexity. It
adds one poll cycle (~3 s) of latency and **zero operator touchpoints**.

If the operator answers §15.3 "no WB in v1", slices 3 and 4 drop and the report
phase is never built; exposure and leveling ship unchanged.

## 9. Horizon leveling and the plugin

**Exact keys:** `CropAngle` (degrees), plus `CropTop`, `CropLeft`, `CropBottom`,
`CropRight` (normalized 0–1), `CropConstrainToWarp`, and the read-only-ish
`HasCrop`. These are the XMP/`crs:` names Lightroom uses for geometry.

**Is straightening reachable through `photo:applyDevelopSettings`? UNVERIFIED,
and I will not assume it.** The SDK documents the settings table as using the
same keys as `getDevelopSettings()`, and crop keys appear there — but whether
`applyDevelopSettings` *honours* geometry keys (versus silently discarding them),
whether a bare `CropAngle` auto-fits or leaves wedge corners, and the sign
convention are all things I cannot verify from this repo. Community reports
conflict. This is precisely the "load-bearing question" CLAUDE.md says to stop on.

**Design around it — Slice 0 is a spike that answers it empirically with no
Python written:**

1. Extract the curated `KEYS` list from `ExportSettings.lua` into a new pure
   `DevelopKeys.lua`, add the six crop keys, and have `ExportSettings.lua`
   require it (single-sourced; `QueueProcessor.lua`'s report phase reuses it in
   slice 3).
2. Add `plugins/epd-edit-bridge/example_straighten.lua` — a hand-written
   settings file for the *existing manual* "Apply Settings from Claude" command
   setting `CropAngle = 1.5` on one frame.
3. Operator: apply → run the export report → read back. Three outcomes:
   **honoured with a usable sign** (leveling ships), **honoured but needs an
   explicit crop rect** (ship with `write_crop_rect:true`), **discarded**
   (leveling is dropped from v1, §15.12).

**If it is discarded, the fallback is NOT to show the operator a number to type
in.** That is a fourth touchpoint. The fallback is: leveling is not shipped;
exposure (and WB) ship alone.

Plugin rules respected throughout: `merge(global, by_file[base])` then **one**
`applyDevelopSettings` call per photo = one undoable history step (unchanged
from today); the queue processor's per-session confirmation text gains a line
naming per-file corrections and develop-settings reads; nothing is obfuscated;
`DevelopKeys.lua` and `JobFile.lua` stay pure and desk-checkable.

## 10. Job-file schema change

`shopsteward.editjob/2` (Python writes `/2` **only when corrections are
enabled**; with `enabled:false` it writes `/1` exactly as today — that is the
rollback path, §16):

```json
{ "schema": "shopsteward.editjob/2", "job_kind": "apply",
  "develop_settings": { "Contrast2012": 12, "Vibrance": 18 },
  "develop_settings_by_file": {
    "IMG_1234": { "Exposure2012": -0.25, "CropAngle": -1.2,
                  "CropTop": 0.016, "CropLeft": 0.011, "CropBottom": 0.984, "CropRight": 0.989 },
    "IMG_1235": { "WhiteBalance": "Custom", "Temperature": 4980, "Tint": 4 } },
  "corrections_config_hash": "…" }
```

`JobFile.validate` gains: `develop_settings_by_file` optional, must be an object
of objects keyed by base name; `job_kind` in `{apply, report}` defaulting to
`apply`; a `report` job may have empty `develop_settings` and needs no `export`
even in mass mode. Result schema bumps to `shopsteward.editresult/2` with an
optional `develop: [{base_name, settings:{…}}]`; `outcomes.py` tolerates both.

**Mixed-version behaviour is deliberately loud.** A `/2` job hitting an old
plugin lands in `failed/` with `malformed: schema is not shopsteward.editjob/1`
— visible and diagnosable. Making `by_file` an additive field on `/1` would make
an old plugin silently ignore every correction, which is worse.

**Values are always absolute, never deltas.** The M2 §4 property — "crash
mid-job re-runs on restart — safe: absolute develop values are idempotent" — is
load-bearing and this design preserves it.

## 11. Feedback capture — capture only, no learning

After Gate 2, `shopsteward edit feedback capture [--since …]` dispatches a
report job for photos in state `edited`, producing
`photo.develop_observed{phase:"post_finish"}`. The projection then holds, per
photo: what was proposed, what was applied, and what the operator kept. That
difference is the training signal a personalized profile would need.

**Explicitly not built in v1:** any fitting, any per-operator profile, any
automatic adjustment of the gain constants, any regression on kept-vs-proposed,
any UI for it. The command is on-demand and manual (§15.11 asks whether the
pipeline's landing scan should trigger it instead — it may, since commands flow
pipeline→editing one-way per decision 27, but v1 proposes not to).

## 12. Events, projection, idempotency

Dot-separated, past tense, immutable, `user_id` on every row.

| Event | Payload |
|---|---|
| `correctionsconfig.seeded` / `.updated` | `{name:"default", config:{…}, config_hash, source:"defaults"\|"operator"}` |
| `corrections.proposed` | `{photo_id, base_name, mode, config_hash, baseline_source:"preset"\|"reported", proposals:{Exposure2012:{raw,delta,value,clamped}, Temperature:{…}, Tint:{…}, CropAngle:{…}, crop_rect?}, abstained:{axis:reason}, measurements:{luma_p99_5, highlight_clip_pct, shadow_clip_pct, rgb_means, hough_lines, angle_iqr}}` |
| `photo.develop_observed` | `{photo_id, base_name, phase:"baseline"\|"post_finish", edit_job_id, settings:{curated KEYS}}` |
| `editjob.dispatched` | *(existing type)* + `corrections_config_hash`, `by_file_count` |

**One event, not three.** A fully-abstained photo emits `corrections.proposed`
with empty `proposals` and a populated `abstained` — that *is* the record that we
looked and declined. Separate `.abstained` / `.applied` types would be three
folds for one fact.

Projection `proj_corrections(user_id, photo_id PK, mode, config_hash,
baseline_source, proposed_json, abstained_json, applied_json, kept_json NULL,
proposed_at, observed_at NULL)`, rebuilt by the existing `rebuild_editing()`.
`kept_json` folds from the `post_finish` observation.

**Re-run over the same folder:** ingest already dedupes by RAW sha256 →
`photo.duplicate_skipped`, no `photo.ingested`, therefore no new proposal. For a
photo that *is* re-examined, the skip predicate is `(photo_id, config_hash)`
already present in `proj_corrections` → skip, unless `--force`. Changing
`corrections.json` changes the hash and legitimately re-proposes. Re-applying the
resulting job in Lightroom is safe because every value is absolute.

## 13. Boundaries

`corrections.py` imports `cv2`, `numpy`, `PIL`, `shopsteward.core.events`, and
`shopsteward.editing.*` only. **No import-linter contract amendment is needed and
none is proposed** — that is a deliberate outcome of choosing CV over a vision
model (§4). New/changed files:

```
src/shopsteward/editing/corrections.py       measure() + propose() (pure) + seed/get config + advance()
src/shopsteward/editing/models.py            + CorrectionProposal, CorrectionsReport
src/shopsteward/editing/dispatch.py          + by_file param; schema /1 vs /2 selection
src/shopsteward/editing/projections.py       + proj_corrections
src/shopsteward/editing/cli.py               + `edit corrections preview|run|status`, `edit feedback capture`
src/shopsteward/adapters/lightroom/interface.py  + EDITJOB_SCHEMA_V2 / RESULT_SCHEMA_V2 constants
src/shopsteward/adapters/lightroom/fake.py       FakeBridge learns by_file + job_kind=report
config/defaults/corrections.json             NEW
plugins/epd-edit-bridge/DevelopKeys.lua      NEW (shared curated KEYS + crop keys)
plugins/epd-edit-bridge/ExportSettings.lua   requires DevelopKeys
plugins/epd-edit-bridge/JobFile.lua          validate /2, job_kind, by_file
plugins/epd-edit-bridge/QueueProcessor.lua   merge by_file; report phase; prompt text
plugins/epd-edit-bridge/example_straighten.lua  NEW (spike instrument)
tests/editing/test_corrections.py            NEW
```

## 14. Milestone placement

**Recommendation: a new milestone row `M2b — Per-image corrections proposer`,
inserted in PRD §10 after M5b in build order but scoped entirely to the editing
module.** Not an M2 amendment: M2 and M3 are shipped and merged, and re-opening
them muddies the "keep PRs scoped to one milestone" rule. Not deferred: mass-mode
per-image WB is arguably the single largest remaining time win for event work,
which is the standalone deliverable M2 exists to serve. M2b carries its own
first-PR operator gate (§8.2) and its own plugin-compatibility marker (schema
`/2`). Proposed row: *"M2b — Per-image corrections proposer: deterministic WB /
exposure / leveling, per-file develop settings, editjob schema v2, feedback
capture. 2 weekends. Gate 2 starts from a corrected frame."*

## 15. OPERATOR DECISIONS — ALL RESOLVED 2026-08-03

| # | Question | Decision |
|---|---|---|
| 1 | Create milestone M2b (§14) | **Yes** |
| 2 | Adopt the §5 guardrail reading as decision 50 | **Yes** (does not affect v1 — no model involved) |
| 3 | Ship white balance in v1 | **NO — exposure + leveling first.** Slices 3–4 become a follow-on, not part of M2b v1. See §20. |
| 4 | Leveling default in mass mode | **OFF** |
| 5 | `enabled` on first merge | **`false`** — merge dark, calibrate, then flip |
| 6 | Bound values | **Accept as proposed**, tune in config after `corrections preview` on real files |
| 7 | Corrections chip on the Gate 1 card | **No** |
| 8 | Plugin/Python lockstep schema bump | **Accepted** |
| 9 | Undo granularity | **Combined — one history step per photo.** The plugin's one-undoable-step rule stands unamended. |
| 10 | New dependencies | **REVERSED 2026-08-03 (later the same day): rawpy is IN.** See §15a. |
| 11 | Feedback capture trigger | **CLI**, on demand |
| 12 | If the §9 spike shows `CropAngle` is unreachable | **Drop leveling from v1.** XMP-sidecar writing is not authorized. |
| 13 | Gain calibration before enabling | **Yes**, operator calibrates locally |

Consequence of decision 3: `white_balance` stays in `corrections.json` with
`enabled: true` as a schema placeholder, but no code path reads it in v1 and the
report phase (§8) is not built. The section is retained because the follow-on
needs it and because removing it would churn the config hash later.

## 15a. Decision 10 reversed — rawpy is in (operator, 2026-08-03)

**New fact the rejection did not have:** the operator may not have a paired JPEG
for every folder. §17 rejected rawpy on two grounds — a new binary dependency,
and that it does not solve the WB-baseline problem. The second ground stands and
is unaffected (Adobe Kelvin is still unrecoverable from WB multipliers, §1b), but
it was never the question here. Without a paired JPEG, `measure()` has **no
pixels to measure at all**, and rawpy is how it gets them.

**Finding that outranks this design.** `editing/ingest.py:129-142`: a RAW with no
matching JPEG emits `photo.unpaired{reason:"missing_jpeg"}` and is skipped — no
`photo.ingested`, so it never enters the pipeline. **RAW-only folders do not
ingest today at all.** That is an M2 ingest gap, not an M2b gap, and it is
strictly larger than corrections: those photos are invisible to scoring, Gate 1,
and everything downstream. Corrections cannot be the place it is fixed.
→ **OPEN OPERATOR DECISION (§15b.1).**

**Decode strategy, when a RAW must supply the pixels:** prefer
`rawpy.imread(path).extract_thumb()` — nearly every RAW carries an embedded
camera JPEG preview, and pulling it costs milliseconds against roughly a second
per frame for a full `postprocess()` demosaic. At mass-mode scale (2,000 frames)
that is the difference between seconds and most of an hour. Fall back to
`postprocess()` only when `extract_thumb()` raises `LibRawNoThumbnailError`.
The embedded preview carries the camera's picture style, exactly as the paired
JPEG already does, so the measurement premise is unchanged rather than newly
biased. Record which source was used in `corrections.proposed.pixel_source`
(`"paired_jpeg" | "raw_thumb" | "raw_postprocess"`) — an exposure statistic is
not comparable across sources, and the feedback ledger will need to know.

## 15b. New open operator decisions

1. **Should RAW-only folders ingest at all?** Today they are silently skipped
   (`photo.unpaired`). Options: (a) ingest them, deriving the JPEG from the RAW's
   embedded preview at ingest time — makes the rest of the system work unchanged,
   and is an M2 change with its own PR; (b) leave ingest alone and have
   corrections read RAWs directly — narrower, but the photos still never reach
   scoring or Gate 1, so it fixes nothing the operator would notice;
   (c) leave as-is and always shoot RAW+JPEG. **(a) is recommended** — it is the
   only option that makes those photos exist to the system, and the measurement
   in item 2 below prices it at **9 ms per frame for a near-full-res JPEG**, so
   the cost objection to deriving at ingest does not survive contact with the
   numbers.
2. ~~**Which camera bodies / RAW formats must be supported?**~~ **RESOLVED
   2026-08-03: Canon R-series, CR3.** rawpy 0.27.0 bundles LibRaw 0.22.1, past
   the 0.21 CR3 threshold, so decode is supported.
   **H.265-preview sub-question: RESOLVED EMPIRICALLY 2026-08-03.** Measured on a
   real operator CR3 (Canon R-series, 8480×5650 sensor), rawpy 0.27.0 /
   LibRaw 0.22.1:

   | Path | Result |
   |---|---|
   | `extract_thumb()` | **`ThumbFormat.JPEG` in 9 ms**, Pillow-openable, **8192×5464** |
   | `postprocess(half_size=True)` | 4111×2744 in 0.91 s |
   | `postprocess()` full | 8222×5488 in 1.14 s |

   The previews are JPEG, not H.265, so the §15a fast path holds. Two findings
   beyond the yes/no: the preview is **near-full-resolution**, not a thumbnail —
   far above the 1600 px `analysis_long_edge_px` the measurement needs, so no
   quality concession is being made; and the fast path is **~125× faster**, which
   on a 2,000-frame event is 18 s against 38 min. Mass mode is viable on RAW-only
   folders only via `extract_thumb()`; a demosaic-per-frame design would not be.
3. **Does rawpy move into the editing module's dependency set** (keeping the
   zero-credentials standalone property — it does, rawpy is offline), and does the
   Windows wheel install cleanly on the operator's machine? Verify before slice 1.

## 16. Guardrail impact and rollback

| Guardrail | Impact |
|---|---|
| Editing-module boundary | **None.** No new forbidden import; no contract amendment. |
| Three gates | **None.** No new operator surface, no accept/reject, no chip (§15.7). Gate 2 gets *less* work. |
| AI never touches the photograph | **Not exercised.** No model in v1; §5 records a reading for the future only. |
| Event-sourced SQLite | Append-only; new types in §12; `proj_corrections` is drop-and-rebuild; `user_id` on every row. |
| Configuration over code | Every bound, gain, deadband and per-mode override in `corrections.json`, DB-seeded with a `config_hash`. Zero magic numbers in Python. |
| Landing-folder handoff | Unchanged; corrections never reach across it. |
| No live external APIs | Zero network on every path, including tests. |
| Public repo | No images committed; all test images synthesized (§19). |
| Plugin rules | One undoable step per photo preserved; session confirmation text amended; `DevelopKeys.lua` pure and inspectable. |
| New dependency | **None proposed** (§15.10). |

**Rollback criteria — all config, no code revert, no event deletion:**

- Set `corrections.enabled: false` → dispatch emits `editjob/1` with no
  `by_file`, byte-identical to today's behaviour. This is the kill switch.
- Per-axis rollback: `exposure.enabled` / `white_balance.enabled` /
  `leveling.enabled` independently false.
- **Trigger:** after the first real event (≥300 frames), if the operator's kept
  values (§11) move *against* the proposal on more than 30% of corrected frames
  for an axis, that axis goes off and its gains get recalibrated before it
  returns.
- **Pre-code trigger:** if the §9 spike shows `CropAngle` is unreachable,
  leveling is cut before slice 2 is written.
- Events already appended stay (append-only); the projection simply stops
  gaining rows.

## 17. Rejected alternatives

- **A vision model proposes the numbers from the JPEG.** Per-frame cost breaks
  mass mode against the $10/mo cap; no offline assertion is possible so the
  logic cannot be unit-tested; and it forces `adapters.vision` into `editing`,
  amending the boundary contract and killing the zero-credentials standalone
  property (PRD §11). Reconsider only with a measured quality gap on real frames.
- **`rawpy`/LibRaw to read as-shot WB multipliers.** A new binary dependency,
  CR3 support contingent on LibRaw ≥ 0.21, **and it does not solve the
  problem** — multipliers → Adobe's Kelvin scale has no published mapping (§1b).
- **Relative deltas in the job file, plugin does the arithmetic.** Destroys the
  M2 idempotency property: a crash-restarted job would add the delta twice. Also
  moves semantics into Lua, against M2's "keep the plugin dumb".
- **A `CorrectionsAdapter` Protocol with one CV implementation.** Interface with
  one implementation; `pipeline/scorers/technical.py` already settles that
  deterministic local CV is a plain module here.
- **A corrections review/accept UI, or an "accept proposals" step in Gate 1.**
  A fourth human touchpoint — forbidden outright by CLAUDE.md.
- **Clamping wild estimates to the maximum instead of abstaining.** Silently
  applies the largest allowed wrong correction; abstaining is strictly safer and
  costs nothing.
- **Straightening by rotating pixels in Pillow on the ShopSteward side.** That
  literally touches the photograph outside Lightroom, breaks RAW-as-print-master,
  and creates a second render path.
- **Auto-crop / recomposition beyond leveling.** Composition is an artistic
  decision; PRD §3.2 keeps those human.
- **Reading Canon MakerNote `ColorTemperature` from the paired JPEG.** Camera
  Kelvin ≠ Adobe Kelvin; would produce confidently wrong absolute values.
- **A daemon watching for finished jobs to auto-advance the report phase.**
  On-demand scanning is the house pattern (decision 24); `advance()` piggybacks
  on the existing `scan_outcomes` call sites.

## 18. Non-goals (explicit)

Local or masked adjustments of any kind; lens/perspective/CA correction;
denoise; sharpening; colour grading beyond WB neutralisation; subject or
face-aware anything; **any learning, fitting, or personalization** (capture only,
§11); crop for composition; multi-image WB/exposure *consistency* across a
sequence (genuinely tempting for a wedding — deliberately deferred, it needs the
proposed-vs-kept ledger first); RAW decoding in Python; a corrections UI;
applying corrections to exported JPEGs rather than through Lightroom; vertical
(architectural) straightening; anything touching the landing folder or the
Etsy pipeline.

## 19. Test plan — zero network, zero committed images

The repo has **no committed photo fixtures and must not gain any** (public repo,
hard guardrail). Every existing image test synthesizes with
`PIL.Image.new` / NumPy (`tests/mockups/helpers.py`, `tests/editing/test_ingest.py`,
`tests/pipeline/listings/helpers.py`). Corrections follows that exactly — so
there is **no licensing story and no repo-size story, because no image is
committed.** Real-file calibration happens locally through
`shopsteward edit corrections preview`, whose output is never committed.

Synthesized fixtures:
- **Exposure:** a flat linear-gray field rendered at a known level → assert
  `ΔEV ≈ log2(target/measured)` within 0.02; a field at target → deadband, delta 0;
  a field 3 stops dark → `|raw| > max_ev × 2` → abstain, not clamp; a field with
  4% highlight clip → positive delta refused by the scene guard.
- **White balance:** a mid-gray field multiplied by known R/B gains → assert
  **sign and monotonicity** of `Δ_temp_K` (not the absolute Kelvin — that is an
  uncalibrated config gain); a pure-red field → mid-luminance band too small →
  abstain `low_confidence`.
- **Leveling:** `cv2.line` drawing a long horizon at a known +1.5° on noise →
  recovered within 0.2°; a flat textureless field → abstain `low_confidence`;
  two long lines at +2° and −2° → IQR exceeds tolerance → abstain; a 12° line →
  abstain rather than clamp to 3°.

**Smallest test that proves the logic works** —
`tests/editing/test_corrections.py::test_known_tilt_is_recovered_bounded_and_abstains_when_wild`:
one synthetic frame tilted +1.5° and one tilted +12°, one config load; assert the
first yields `CropAngle` matching within 0.2° with `|angle| ≤ max_rotation_deg`,
and the second yields `abstained == {"leveling": "out_of_bounds"}` with no
`CropAngle` in the proposal. If the measurement, the bounding, the abstain
branch, or the config plumbing breaks, that one test fails.

**E2E** (`tests/editing/test_e2e_corrections.py`, extends the existing mass-mode
E2E): mass ingest 3 synthetic pairs → `corrections run` → assert three
`corrections.proposed` events with at least one abstention → assert the
dispatched job file is `schema: shopsteward.editjob/2` and its
`develop_settings_by_file` carries per-base-name keys and no key that also
appears in the global block with a conflicting value → `FakeBridge.consume_all()`
→ `proj_corrections` populated → re-run → zero new proposals (idempotent) →
change `corrections.json`, re-run → exactly three new proposals with a new
`config_hash`. Assert zero network and zero `llm.call` events.

## 20. Implementation slices (dependency order)

| # | Slice | Size | Mergeable independently |
|---|---|---|---|
| **0** | **FIRST PR.** `DevelopKeys.lua` (shared curated keys + `CropAngle`/crop rect/`CropConstrainToWarp`), `ExportSettings.lua` requires it, `example_straighten.lua`, `TESTING.md` spike checklist. **Answers §9: is straightening reachable at all.** No Python. | 1 evening | Yes — improves the export report on its own. |
| **1** | `corrections.py` pure `measure()`/`propose()` + `config/defaults/corrections.json` + `correctionsconfig.seeded` + `corrections.proposed` + `proj_corrections` + `shopsteward edit corrections preview <path>` (dry-run, no dispatch, no writes) + full synthetic test suite. **Not wired into dispatch.** | 1 weekend | Yes — the calibration tool ships alone. |
| **2** | `editjob/2` + `develop_settings_by_file`: `JobFile.validate`, `QueueProcessor.lua` merge, `FakeBridge`, `EditJobSpec`, `dispatch.py`, `/1`-vs-`/2` selection on `enabled`. Wires **exposure** (+ leveling iff slice 0 passed) into hero and mass. E2E. | 1 weekend | Yes — corrections work end to end, WB absent. |
**M2b v1 ends at slice 2** (operator decision 3, 2026-08-03). The two slices
below are a deliberate follow-on, not scope cut in a panic — they are specced
here so the follow-on needs no new design pass, and because slice 2's schema
must not foreclose them.

| # | Slice (FOLLOW-ON, not M2b v1) | Size | Mergeable independently |
|---|---|---|---|
| **3** | Report phase: `job_kind:"report"`, `editresult/2`, `photo.develop_observed{baseline}`, `advance()` at the existing `scan_outcomes` call sites, WB proposals off the reported baseline. | 1 weekend | Yes — adds WB to a working system. |
| **4** | *Blocked on slice 3.* Feedback capture: `shopsteward edit feedback capture`, `phase:"post_finish"`, `proj_corrections.kept_json`, proposed-vs-kept counts in `edit status`. **No learning.** | 1 evening | Yes. |

## 21. PRD §13 decision-log amendment (candidates 50–56)

```
M2b design (2026-08-03; normative spec at
docs/designs/2026-08-03-corrections-proposer.md):

50. "AI never touches the photograph" reading: the rule forbids any model that
    EMITS OR MODIFIES PIXELS of a photograph — generative edit, inpaint/fill,
    sky replacement, model upscale, model denoise, model-generated masks driving
    local adjustments, and any model output that becomes part of the delivered
    file. A model that emits NUMERIC develop parameters interpreted by
    Lightroom's own deterministic raw engine as a reversible history step is not
    covered (same class as the vision score of decision 22; consistent with
    decision 34). M2b v1 does not exercise this: the corrections proposer is
    deterministic CV with no model.
51. The corrections proposer is deterministic OpenCV/NumPy inside
    src/shopsteward/editing/corrections.py — a plain module, NOT an adapter
    Protocol (the pipeline/scorers/technical.py precedent; no interface with one
    implementation). No new dependency: cv2/numpy/Pillow are already declared;
    rawpy/LibRaw is rejected. This keeps the editing module standalone with zero
    credentials and requires no import-linter contract amendment.
52. Corrections are AUTO-APPLIED, bounded, and never reviewed. There is no
    accept/reject surface anywhere — that would be a fourth touchpoint. Bounds,
    deadbands, gains, abstain factors and per-mode overrides live in
    config/defaults/corrections.json, DB-seeded with a config_hash. An estimate
    exceeding max × abstain_factor ABSTAINS rather than clamping to the maximum;
    an abstained axis is silently left alone and recorded in the event log only.
53. Lightroom Temperature is absolute Kelvin on a camera-specific Adobe scale
    and cannot be derived from the paired JPEG or from Canon MakerNotes.
    Per-image white balance therefore requires a read-back round trip: a
    job_kind="report" bridge job returns getDevelopSettings() as
    photo.develop_observed{phase:"baseline"}, and the proposal is
    baseline + bounded delta. Exposure and leveling need no round trip. All
    job-file values stay ABSOLUTE — never deltas — preserving the M2 §4
    crash-restart idempotency property.
54. Job-file schema bumps to shopsteward.editjob/2 (optional
    develop_settings_by_file keyed by base name; job_kind apply|report) and
    result to shopsteward.editresult/2 (optional develop[]). Python writes /1
    when corrections.enabled is false, so disabling corrections is a complete
    rollback with no code revert. One applyDevelopSettings call per photo is
    preserved = one undoable history step.
55. Horizon leveling targets CropAngle (+ CropTop/Left/Bottom/Right,
    CropConstrainToWarp). Whether photo:applyDevelopSettings honours geometry
    keys is UNVERIFIED and is settled by a Lightroom spike (slice 0) before any
    Python is written; if it does not, leveling is dropped from v1 rather than
    surfaced to the operator as a number to apply by hand.
56. Mode split: hero gets wider bounds (Gate 2 always reviews it); mass gets
    tighter exposure/WB bounds and leveling OFF by default (unreviewed output,
    inscribed-crop loss across a whole gallery, highest Hough false-positive
    rate). Gate 2 feedback (what the operator kept) is CAPTURED in v1 via the
    same report mechanism and proj_corrections; no learning, fitting, or
    personalization is built.
```
