# M4 Implementation Plan — Templates + Compositor

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Spec detail: `docs/designs/2026-07-04-m4-templates-compositor.md`. Branch `m4/templates-compositor`. TDD; reviewer pass before PR; PR = operator gate.

**Goal:** Valid landing files get complete six-intent mockup sets on disk, deterministically, provable in CI with synthetic templates and zero network.

### Task A — models, config, template registry, placeholders (design §1–§3)
mockups/{models,config,templates}.py; settings dirs; scripts/make_placeholder_templates.py (run it; commit outputs: 3 single-region rooms varying color/orientation + 1 two-region gallery wall, 1600px JPEGs + sidecars); import-linter "mockups is imported by no lower layer" contract. Tests: sidecar validation matrix (convex/bounds/size/ppi/duplicate), scan idempotency by hash, update on sidecar change, avg_hue computed.
Commit: `feat(mockups): staging template registry, sidecar schema, synthetic placeholders`

### Task B — compositor core + intents + selection (design §4 §5)
compositor.py (pure: warp, scale-from-inches, fit+mat, light match w/ clamps, shadow, frame), intents.py (six renders), selection.py. Tests: checkerboard warp corner assertions; scale math (24in print in 36in region occupies ~2/3 top edge px); mat never crops (aspect-mismatch case); light-match clamps honored; each intent produces plausible output on placeholder templates (pixel sanity per design §7); selection orientation/diversity/tie rules.
Commit: `feat(mockups): pure compositor, six intent renders, template selection`

### Task C — jobs orchestrator + projections + CLI + API + E2E (design §3 §6)
jobs.py (set_key idempotency, manual-drop photo_ref, --force), projections.py, cli.py, api.py (path-validated FileResponses), mount router + CLI. E2E per design §7 incl. AdobeRGB TIFF master.
Commit: `feat(mockups): mockup job orchestration, projections, CLI/API`

### Task D — frontend Templates + Mockups pages (design §6)
Templates.tsx (registry + 4-corner annotator + scale hint), Mockups.tsx (set gallery), App tabs, api.ts additions. npm build green.
Commit: `feat(ui): template annotator and mockup gallery`

### Task E — wrap-up
Final reviewer pass; suite+ruff+lint-imports(3 contracts)+frontend green; CLI smoke; PR (operator review).
