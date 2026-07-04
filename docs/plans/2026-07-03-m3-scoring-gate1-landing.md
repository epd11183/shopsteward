# M3 Implementation Plan — Scoring, Gate 1, Landing Folder

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. All spec detail lives in `docs/designs/2026-07-03-m3-scoring-gate1-landing.md` — tasks reference its sections. Branch `m3/scoring-gate1-landing`. TDD throughout; reviewer pass before PR; PR = operator gate.

**Goal:** Hero photos flow: awaiting_scoring → scored (fixture vision) → Gate 1 queue → keyboard curation → hero edit job dispatched → landing-folder pickup validated — all CI-provable with zero network.

### Task A — foundations: tuning profile, models, imaging, contracts (design §1 §3 §6)
tuning.py + models.py + imaging.py + config/defaults/tuning_profile.json + prompts/commercial_score.txt; settings.landing_dir(); import-linter contract additions (amend editing contract + new pipeline layering contract). Tests: seed idempotent, get_profile, prep_vision_jpeg strips ALL EXIF + ≤1024px, contracts KEPT.
Commit: `feat(pipeline): tuning profile store, vision image prep, layering contracts`

### Task B — vision adapters (design §4)
adapters/vision/{interface,gemini,fake}.py. Gemini via httpx + respx tests (request shape asserted: x-goog-api-key header, inlineData b64, responseSchema, usage parse, parse-failure raises). Fixture adapter deterministic; Fake programmable. NO live path wired anywhere yet.
Commit: `feat(adapters): vision adapter protocol — Gemini (httpx, unwired), fixture, fake`

### Task C — scorers + scoring orchestrator (design §2 §3)
scorers/ (registry, technical via OpenCV, commercial wrapper, stubs) + scoring.py (composite, ±band Pro escalation, photo.scored/queued/score_failed, llm.call on live only, idempotent by proj_scores) + projections.py (4 tables) + `score run` CLI (+ triple live gate refusal) . Tests: technical scorer on synthetic sharp/blurred/over-exposed Pillow images; composite math incl. None-scorer exclusion; borderline escalation; idempotent re-run; live-gate refusal.
Commit: `feat(pipeline): scoring pipeline with technical + commercial scorers and Gate 1 queueing`

### Task D — gate1 + landing + API + E2E (design §2 §5)
gate1.py (decide/undo incl. best-effort job recall), landing.py, api.py (6 endpoints incl. preview FileResponse), cli.py (`pipeline scan|status`), mount router. E2E test per design §7. Tests: approve dispatches hero job via existing editing.dispatch; undo pending-job recall true/false paths; landing match/manual-drop/invalid; decisions idempotent-safe.
Commit: `feat(pipeline): Gate 1 decisions, landing-folder scan, pipeline API`

### Task E — Gate 1 UI (design §5)
frontend/src/pages/Gate1.tsx + App tab + api.ts types/functions. Keyboard A/R/S/Z, auto-advance, preload, snoozed drawer, dispatch chip. `npm run build` green.
Commit: `feat(ui): Gate 1 keyboard-first curation card`

### Task F — wrap-up
Final reviewer pass (full branch, contract cross-check FakeVision vs design, layering, guardrails), suite+ruff+lint-imports+frontend green, CLI smoke, PR (operator review).
