# ShopSteward

Modular workflow tool for a photography business.

- **Editing module** (standalone): ingest a folder of mixed RAW + JPEG files,
  score, apply preset families via the EPD Edit Bridge Lightroom plugin,
  export. Two modes: **hero** (single-image, feeds the Etsy pipeline) and
  **mass** (batch, event work — weddings, races, sports; no Etsy coupling).
- **Etsy pipeline**: watches a landing folder for photos flagged for sale,
  then handles viability scoring, staging-template mockups, Etsy listing
  drafts (digital direct; physical via Printful and Gelato), Instagram
  promotion, and a performance feedback loop into tuning profiles.

**The operating principle for the hero/Etsy path: three gates.** Gate 1
Curate (approve/reject scored candidates), Gate 2 Finish (quick Lightroom
pass; export = approval), Gate 3 Publish (one-tap listing + IG approval).
Everything between must run unattended. If a feature adds a fourth human
touchpoint in the hero path, it is wrong — redesign it.

**Mass mode has its own flow** — see `docs/PRD_v2.1.md` §4.2.

Full spec: `docs/PRD_v2.1.md` (v2.1 supersedes v2). When PRD and this file
disagree, the PRD wins; flag the discrepancy.

## Architecture rules (non-negotiable)

- **Monolithic core, pluggable adapters.** The core owns the data model and
  orchestration. Every external system (Etsy, Printful, Gelato, Instagram,
  Lightroom, scene generators, vision scoring) sits behind an adapter
  interface in `src/shopsteward/adapters/`. Core code never imports an SDK
  directly.
- **Editing module boundary.** `src/shopsteward/editing/` is standalone. It
  must not import from `adapters/etsy`, `adapters/printful`,
  `adapters/gelato`, `adapters/instagram`, or `pipeline/`. An import-linter
  rule enforces this in CI from M2 onward.
- **Folder-pointed ingestion.** Discovery is invoked with
  `shopsteward ingest <path> --mode {hero,mass}`. The folder contains paired
  RAW + JPEG files; the ingester pairs them by base filename. No static
  watch folder.
- **Landing folder is the only Etsy handoff.** Photos flagged for Etsy land
  in a configured folder; the Etsy pipeline watches only that folder. No
  in-process call from editing → Etsy pipeline, ever.
- **Event-sourced SQLite.** Events are immutable and append-only; derived
  state is rebuilt via projections. Never UPDATE or DELETE an event row.
- **Configuration over code.** Tuning profiles, scoring weights, routing
  rules, copy templates, staging-template metadata, and pricing rules live
  in the database (seeded from `config/defaults/`), never hardcoded in
  Python.
- **POD-first listing creation for physical SKUs.** Gelato/Printful APIs
  create the product and push the Etsy draft; we then *enrich* the draft
  (title, tags, description, images, price) via the Etsy API. Never modify
  provider-set SKU values or variation structure.
- **AI never touches the photograph.** Vision models score and generate
  empty-room templates; the print is composited deterministically with
  Pillow/OpenCV. No generative edit, upscale, or fill on a photograph, ever.
- **user_id foreign key on every major table** — multi-tenant readiness,
  even though v1 is single-operator.

## Development workflow (non-negotiable)

- **All non-trivial implementation runs in sub-agents** defined under
  `.claude/agents/`. The main session orchestrates, presents diffs, and
  gates decisions to the operator. Roster: `architect`, `python-impl`,
  `test-author`, `reviewer`, `lua-impl`. See PRD §8.1.
- **`reviewer` sub-agent runs before the operator sees any diff.** It
  checks output against these guardrails and the current PRD milestone.
- **Operator review is required** for architecture changes, adapter
  interface changes, amendments to CLAUDE.md / PRD / `.claude/settings.json`,
  new dependencies, new external services, AI model/provider selection,
  anything touching secrets, and the first PR of every milestone. Format:
  see PRD §8.2 and `KICKOFF.md` §1.2.
- **C-Suite critique before finalizing major designs.** CTO / CFO / CMO /
  CPO / Chief Legal, 2–4 sentences each, at least one concrete improvement
  proposal per voice. See PRD §8.3.
- **No live external APIs by default.** Adapters are exercised against
  recorded, scrubbed fixtures until the operator approves a specific smoke
  test for a specific provider.

## Repo layout

```
src/shopsteward/editing/    standalone editing module (no Etsy imports)
src/shopsteward/pipeline/   Etsy pipeline: mockups, listings, IG, feedback
src/shopsteward/core/       data model, event store, shared services
src/shopsteward/adapters/   one module per external system
plugins/epd-edit-bridge/    Lightroom Classic Lua plugin (own README)
config/defaults/            shippable defaults: tuning profiles, templates,
                            staging templates, prompts
data/                       runtime data (DB, caches, exports) — gitignored,
                            never read it
docs/                       PRD and design docs; PRD_v2.1.md is current
tests/                      pytest; adapters tested against fixtures
.claude/agents/             sub-agent definitions
```

## Commands

- `uv sync` — install deps
- `uv run pytest` — tests (must pass before any commit)
- `uv run ruff check . && uv run ruff format .` — lint/format
- `uv run shopsteward serve` — FastAPI backend + local UI
- `uv run shopsteward ingest <path> --mode {hero,mass}` — folder-pointed
  ingestion
- `uv run shopsteward edit <path> [options]` — standalone editing invocation
  (event work)

## Conventions

- Python 3.12, FastAPI, Pydantic v2 models everywhere at boundaries.
- Type hints required; `ruff` clean; no bare `except`.
- Adapter fixtures: record real API responses once, scrub identifiers,
  commit the scrubbed fixture. Never commit a raw API response.
- Frontend: React + Vite in `frontend/`, Tailwind, no component-library
  sprawl.
- Lua plugin changes: keep the plugin inspectable — no obfuscation, one
  undoable Lightroom history step per apply, confirmation prompts on writes.

## Hard guardrails

- **Never** read, print, or commit anything under `data/` or any `.env*`
  file.
- **Never** commit real shop data, credentials, photo files, or API fixtures
  with live identifiers. This repo is public.
- **Never** call live external APIs in tests; adapters get fakes/fixtures.
- **Never** assume an answer to a load-bearing question — stop and ask.
- Destructive git (force-push, hard reset) and `rm -rf` are off the table.
- This is a nights-and-weekends project alongside a full-time Workiva role
  — prefer boring, maintainable choices over clever ones. Ask before adding
  a new dependency or service.

## Current focus

M0 scaffold → M1 (Etsy data pull + analytics dashboard). The revised
milestone order (M2 = standalone editing module, M3 = hero-mode + landing
folder) is in `docs/PRD_v2.1.md` §10. Check the milestone table before
starting new work, and keep PRs scoped to one milestone.

All 16 open questions from `KICKOFF.md` §2 were resolved on 2026-07-03 and
are folded into PRD v2.1 (see its §13 for the decision log). No stage is
blocked on open questions.
