# M3 Design — Hero Scoring, Gate 1 Curation, Landing-Folder Handoff

*Architect output, approved by operator 2026-07-03 (hero preset = tuning-profile
`hero_preset_family` seeded "neutral"; snooze = indefinite visible shelf; PRD §5.2
landing wording amended — see PRD §13 decisions 25–27; scoring/threshold decisions
21–24).*

Extends the M2 patterns exactly: append-only `core.events`, drop-and-rebuild
projections with module-local `rebuild_*()`, Protocol adapters with fixture/fake
defaults, on-demand scans (no daemons), Typer sub-apps, APIRouter mounted by
top-level `api.py`.

## 1. File map

```
src/shopsteward/pipeline/__init__.py
src/shopsteward/pipeline/models.py        Pydantic v2: TuningProfile, ScoreBreakdown, VisionVerdict,
                                          Gate1Card, LandingReport.
src/shopsteward/pipeline/tuning.py        Tuning-profile store: seed config/defaults/tuning_profile.json
                                          → tuningprofile.seeded event; get_profile() reads projection.
                                          Same shape as editing/presets.py.
src/shopsteward/pipeline/imaging.py       prep_vision_jpeg(jpeg_path) -> bytes: Pillow in-memory downscale
                                          to 1024px long edge + FULL EXIF/GPS strip (re-encode, no metadata).
src/shopsteward/pipeline/scorers/__init__.py   Registry: ordered dict name -> Scorer; register() at import.
src/shopsteward/pipeline/scorers/technical.py  OpenCV/Pillow: Laplacian sharpness, exposure histogram,
                                               noise estimate; normalization curves from tuning profile.
src/shopsteward/pipeline/scorers/commercial.py Wraps a VisionAdapter; returns score + verdict + usage.
src/shopsteward/pipeline/scorers/stubs.py      catalog_gap, historical_conversion -> None (weight 0).
src/shopsteward/pipeline/scoring.py       Orchestrator: unscored awaiting_scoring photos → scorers →
                                          composite → borderline Pro escalation → photo.scored/queued +
                                          llm.call (live only) + photo.score_failed.
src/shopsteward/pipeline/gate1.py         Queue read; decide(approve|reject|snooze); undo (best-effort job
                                          recall); approve calls editing.dispatch.dispatch_edit_job(mode=hero).
src/shopsteward/pipeline/landing.py       On-demand landing_dir() scan: technical validation ONLY;
                                          base-name match to proj_photos; landing.* events. Stops there (M4).
src/shopsteward/pipeline/projections.py   proj_scores, proj_gate1, proj_landing_files, proj_tuning_profiles;
                                          rebuild_pipeline(). Never writes editing's tables.
src/shopsteward/pipeline/api.py           APIRouter /api/pipeline.
src/shopsteward/pipeline/cli.py           `shopsteward score` + `shopsteward pipeline` sub-apps.
src/shopsteward/adapters/vision/interface.py  VisionAdapter Protocol + VisionVerdict/Usage/Result.
src/shopsteward/adapters/vision/gemini.py     GeminiVisionAdapter — httpx only, REST generateContent.
src/shopsteward/adapters/vision/fake.py       FixtureVisionAdapter (deterministic) + FakeVisionAdapter (tests).
config/defaults/tuning_profile.json       weights/threshold/band/technical curves/landing minimums/vision cfg.
config/defaults/prompts/commercial_score.txt   Scoring prompt (config-over-code).
src/shopsteward/settings.py               + landing_dir() (SHOPSTEWARD_LANDING_DIR, default data/landing).
frontend/src/pages/Gate1.tsx              keyboard-first single-card UI; App.tsx "Gate 1" tab.
tests/pipeline/…                          incl. test_e2e_hero_gate1.py.
```

## 2. Events, payloads, projections

| Event | Payload |
|---|---|
| `tuningprofile.seeded` / `.updated` | `{name:"default", profile:{…}, source:"defaults"\|"operator"}` |
| `photo.scored` | `{photo_id, profile_name, scores:{technical, commercial, catalog_gap:null, historical_conversion:null}, composite, escalated, vision:{triage:{model,verdict}, rescore:{model,verdict}\|null}}` |
| `photo.queued` | `{photo_id, composite}` — iff composite ≥ threshold |
| `photo.score_failed` | `{photo_id, scorer, error:{code,message}}` — stays eligible, retried next run |
| `llm.call` | `{provider, model, purpose:"commercial_triage"\|"borderline_rescore", photo_id, input_tokens, output_tokens, est_cost_usd}` — LIVE calls only; emitted by scoring.py (adapters never touch the DB) |
| `gate1.approved` | `{photo_id, composite, edit_job_id}` (editing emits editjob.dispatched itself) |
| `gate1.rejected` / `gate1.snoozed` | `{photo_id}` |
| `gate1.undone` | `{photo_id, undo_of, job_recalled: bool}` |
| `landing.file_observed` | `{file_id (sha256), path, base_name, format, width, height, color_space, photo_id\|null}` |
| `landing.file_invalid` | `{path, reason:"unreadable"\|"below_min_resolution"\|"unknown_color_space"\|"unsupported_format"}` |

Projections (rebuild_pipeline, drop/rebuild, user_id everywhere):
- `proj_tuning_profiles(user_id, name, profile_json)` last-write-wins.
- `proj_scores(user_id, photo_id PK, technical, commercial, catalog_gap, historical_conversion, composite, escalated, subject, strongest_room_style, one_risk, rationale, model_used, scored_at)`.
- `proj_gate1(user_id, photo_id PK, state pending|approved|rejected|snoozed, composite, decided_at, edit_job_id NULL)` — from photo.queued + gate1.* folds; undo → pending. Queue = pending ORDER BY composite DESC; shelf = snoozed.
- `proj_landing_files(user_id, file_id PK, path, base_name, photo_id NULL, format, width, height, color_space, status valid|invalid, reason NULL)`.

**Ownership rule:** pipeline never writes proj_photos. Unscored candidates =
`proj_photos.status='awaiting_scoring' AND photo_id NOT IN proj_scores`.
proj_photos.status stays `awaiting_scoring` for hero photos permanently (by
design; UI state is a join).

## 3. Scorer registry + tuning profile

```python
class Scorer(Protocol):
    name: str
    def score(self, ctx: ScoreContext) -> ScorerResult | None  # None => excluded
```
Weight-0 scorers skipped entirely (no cost). Composite = Σwᵢsᵢ/Σwᵢ over
weight>0 scorers that returned a value, clamped 0–100. Commercial failure ⇒
photo.score_failed, NO technical-only composite.

`config/defaults/tuning_profile.json`:

```json
{ "schema": "shopsteward.tuning/1", "name": "default",
  "scoring": {
    "weights": {"technical": 0.35, "commercial": 0.65, "catalog_gap": 0.0, "historical_conversion": 0.0},
    "gate1_threshold": 60, "borderline_band": 10,
    "hero_preset_family": "neutral",
    "technical": {"laplacian_floor": 50, "laplacian_ceiling": 1500,
                  "clip_shadow_pct_max": 2.0, "clip_highlight_pct_max": 1.0,
                  "noise_sigma_ceiling": 12.0, "min_long_edge_px": 4000} },
  "vision": {"triage_model": "gemini-2.5-flash", "rescore_model": "gemini-2.5-pro",
             "max_long_edge_px": 1024,
             "est_cost_per_mtok": {"gemini-2.5-flash": {"in": 0.30, "out": 2.50},
                                   "gemini-2.5-pro": {"in": 1.25, "out": 10.00}},
             "monthly_soft_cap_usd": 10.0},
  "landing": {"min_long_edge_px": 3000, "allowed_formats": ["TIFF", "JPEG"]} }
```

Escalation: technical → Flash commercial → composite; if within threshold ±
band → ONE Pro re-score replaces commercial, recompute, escalated=true.
Scored photos never re-scored (idempotent via proj_scores).

## 4. Vision adapter

VisionVerdict {commercial_score int 0-100, subject, strongest_room_style,
one_risk, rationale ≤140 chars}; VisionUsage {model, input_tokens?,
output_tokens?, est_cost_usd?}; VisionResult {verdict, usage|None}.
`score_commercial(jpeg_bytes, *, model) -> VisionResult`.

GeminiVisionAdapter: `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`,
header `x-goog-api-key`, body: contents[0].parts = [inlineData{mimeType
image/jpeg, data b64}, text prompt]; generationConfig {temperature 0,
responseMimeType application/json, responseSchema OBJECT with the 5 required
fields}. Parse candidates[0].content.parts[0].text as JSON → Pydantic; usage
from usageMetadata; est_cost from tuning pricing table. **Uncertainties to
verify at fixture-recording**: exact camelCase field names, current model ID
strings, responseSchema type casing. Parse failure raises → score_failed;
never a guessed score.

FixtureVisionAdapter (default): verdicts keyed by base_name from
tests/fixtures/vision/*.json; unknown names → deterministic pseudo-verdict
from sha256(base_name) (score 30–90); usage None → no llm.call events.
FakeVisionAdapter: programmable queue of results/exceptions.

**Live gate (§8.4):** live requires `--live-vision` flag AND
`SHOPSTEWARD_LIVE_VISION=1` AND `GEMINI_API_KEY`; else red refusal. Same in
API. Before each live call: sum current-month llm.call est_cost_usd; refuse
past monthly_soft_cap_usd; warn log at 80%.

## 5. API / CLI / UI

API `/api/pipeline`: POST /score/run {limit?, live_vision?:false} →
{scored, queued, escalated, failed} (403 unless env gate open); GET
/gate1/queue?state=pending|snoozed → Gate1Card[] {photo_id, composite,
scores, subject, strongest_room_style, one_risk, rationale, escalated,
dispatch_state}; GET /gate1/photo/{photo_id}/preview → FileResponse of
paired JPEG; POST /gate1/decide {photo_id, decision}; POST /gate1/undo
{photo_id}; POST /landing/scan → LandingReport.

Approve path: dispatch_edit_job(photo_ids=[id], preset_family=profile
hero_preset_family, mode="hero", event_name=None ⇒ "ShopSteward — Needs
Finishing", export=None) → gate1.approved{edit_job_id}. Undo approve:
best-effort recall — delete job file from bridge inbox if unconsumed
(job_recalled true/false); hero job is benign if already consumed.

CLI: `shopsteward score run [--limit N] [--live-vision]`;
`shopsteward pipeline scan`; `shopsteward pipeline status`. No gate1 CLI —
UI is the decision surface.

UI Gate1.tsx: single card — preview, composite badge (escalated marker),
rationale line, subject/room-style/risk chips, tech-vs-commercial mini-bars.
Keys A/R/S, Z undo, auto-advance, next-image preload. Snoozed drawer
(re-queue on click). Post-approve inline "dispatched → Lightroom" chip.

Landing: on-demand; new files = TIFF/JPEG whose sha256 not in
proj_landing_files; Pillow open+verify, long edge ≥ min, color space
detectable; match when file stem startswith a known base_name; else manual
drop (photo_id null). Gate 2 export preset must keep base_name as filename
prefix (documented constraint).

## 6. import-linter contracts

Amend "editing module is standalone": add `shopsteward.adapters.vision` to
forbidden_modules. Add:

```toml
[[tool.importlinter.contracts]]
name = "pipeline is imported by no lower layer"
type = "forbidden"
source_modules = ["shopsteward.core", "shopsteward.editing",
                  "shopsteward.adapters.lightroom", "shopsteward.adapters.vision"]
forbidden_modules = ["shopsteward.pipeline"]
```

## 7. Risks + E2E

1. Gemini REST shape drift → Pydantic-validated, hard-fail, fixture-recording verifies.
2. Technical curves uncalibrated → all in tuning profile; calibrate on real exports.
3. Approve-undo vs Lua race → best-effort recall + benign hero semantics + job_recalled flag.
4. Landing base-name mismatch if export preset renames → constraint documented; degrades to manual drop.
5. Cost runaway → one escalation/photo/run, never re-score, soft-cap check, ledger from call #1.

E2E (tests/pipeline/test_e2e_hero_gate1.py, zero network): hero ingest 3 →
score run with FakeVision (pass/fail/borderline w/ Pro second call) → events
+ projections → gate1 approve w/ FakeBridge → hero editjob.dispatched
(export None, Needs Finishing) → landing scan (match + manual drop +
invalid) → assert zero llm.call events.

## 8. Rejected alternatives

Daemons (on-demand is the house pattern); google-genai SDK (decision 22,
inspectability); pipeline writing proj_photos (cross-module rebuild
coupling); batch dispatch at session end (fourth-touchpoint flavor,
contradicts "on operator approval").
