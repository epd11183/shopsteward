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

**Superseded 2026-08-09 (see "Current focus").** The automated hero/Etsy
gating — **Gate 1 Curate** (scored candidates) and its scoring pipeline — was
removed; the operator now culls winners manually. The core is the mass-mode
RAW auto-edit engine. Shop-building (the former Gate 3 Publish: one-tap
listing + IG) is **deferred**, its adapters parked, to be resourced later from
a manual winners folder. The historical three-gate model below is kept for
context only:

> _The operating principle for the hero/Etsy path: three gates. Gate 1 Curate
> (approve/reject scored candidates), Gate 2 Finish (quick Lightroom pass;
> export = approval), Gate 3 Publish (one-tap listing + IG approval).
> Everything between must run unattended._

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
  `adapters/gelato`, `adapters/meta`, or `pipeline/`. An import-linter
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
- **Operator review boundary — see "Governance & decision authority" below
  (rewritten 2026-08-24).** Architecture changes, adapter interface
  changes, new dependencies, new external services, and AI model/provider
  selection are **normal delegated implementation decisions**, not
  approval gates, as of the 2026-08-24 governance rewrite. Format for the
  PR itself (not an approval request): see PRD §8.2 and `KICKOFF.md` §1.2.
- **C-Suite critique before finalizing major designs.** CTO / CFO / CMO /
  CPO / Chief Legal, 2–4 sentences each, at least one concrete improvement
  proposal per voice. See PRD §8.3. This is a self-review engineering
  control (catches design gaps before code is written), not an approval
  gate — it does not block on the operator.
- **Adapters are fixture-first, live-second, by engineering practice, not
  by approval gate.** Land `interface.py`/`models.py`/`fake.py` and their
  tests before `live.py`; going live is then a normal delegated decision
  once fixtures pass and any required credential exists (credential
  acquisition is often a natural bottleneck — e.g. an account only a human
  can create — not a governance gate in itself).

## Commands

- `uv run shopsteward serve` — FastAPI backend + local UI
- `uv run shopsteward ingest <path> --mode {hero,mass}` — folder-pointed
  ingestion
- `uv run shopsteward edit <path> [options]` — standalone editing invocation
  (event work)

## Conventions

- Python 3.12, FastAPI, Pydantic v2 models everywhere at boundaries.
- Type hints required; `ruff` clean; no bare `except`. Tests must pass
  before any commit.
- Adapter fixtures: record real API responses once, scrub identifiers,
  commit the scrubbed fixture. Never commit a raw API response.
- Frontend: React + Vite in `frontend/`, Tailwind, no component-library
  sprawl.

## Hard guardrails

- **Never** read, print, or commit anything under `data/` or any `.env*`
  file.
- **Never** commit real shop data, credentials, photo files, or API fixtures
  with live identifiers. This repo is public.
- **Never** call live external APIs in tests; adapters get fakes/fixtures.
- **Never** assume an answer to a question that crosses an Operator Review
  Boundary (see "Governance & decision authority") — stop and ask. A
  decision that is merely *important* is not, by itself, a load-bearing
  question — see the Decision Priority hierarchy below.
- Destructive git (force-push, hard reset) and `rm -rf` are off the table.
- This is a nights-and-weekends project alongside a full-time Workiva role
  — prefer boring, maintainable choices over clever ones regardless of
  approval status.

## Governance & decision authority

**Rewritten 2026-08-24, explicit operator authorization.** Governing
principle: **ShopSteward has authority to make and execute any reversible
decision within the defined business scope, available budget, and existing
security boundaries without requesting operator approval. Human review is
the exception, not the default.**

**Decision priority, applied to every proposed action, in order:**

1. Is it prohibited by law/platform policy? → Do not execute.
2. Does it cross an explicit Operator Review Boundary (below)? → Escalate.
3. Is it technically executable with available capabilities? → Execute.
4. Is a capability missing? → Request the capability, not the decision
   (report: what's missing, why it's needed, what would enable it, what
   happens once it exists — never silently substitute a worse option).

There is no fifth state called "ask the operator because this seems
important."

**Operator Review Boundaries — human approval is required ONLY when an
action crosses one of these:**

- **Financial boundary** — creates a new recurring cost or expenditure
  outside the authorized budget (see Financial governance below).
- **Security boundary** — changes authentication, credentials, secrets
  management, account ownership, MFA, payout information, or materially
  weakens security.
- **Destructive-data boundary** — could irreversibly delete or corrupt
  meaningful production data and cannot reasonably be restored (the
  event-sourcing rule above is the main engineering control against this —
  events are never UPDATEd/DELETEd, so almost nothing here is actually
  irreversible at the data layer).
- **Legal/platform boundary** — a meaningful copyright, trademark, tax,
  regulatory, contractual, privacy, or platform-policy question requiring
  owner judgment.
- **Business-identity boundary** — changes the legal entity, banking, Etsy
  ownership, fulfillment-provider ownership, tax identity, or public
  identity of the business.
- **Budget-expansion boundary** — requires spending authority beyond what
  the operator has explicitly granted.
- **Truly irreversible high-impact action** — substantial downside that
  cannot reasonably be undone.

**Explicitly NOT boundaries — normal delegated implementation/business
decisions, made on expected value, reliability, maintainability, cost, and
risk, without asking first:** architecture changes, internal refactoring,
adapter interface changes, addition or replacement of ordinary software
dependencies, selection or replacement of AI models/providers, addition of
external services, database/schema changes that are safely reversible
(event-sourced — see above), scraping implementation changes, marketing
strategy, listing strategy, SEO strategy, pricing changes, product
selection, social content, experiments, scheduling, normal operational
configuration.

For changes with some operational risk that are NOT a hard boundary, use
engineering controls instead of human approval: backups, migrations,
feature flags, tests, staged rollout, rollback plans, canaries, dry runs,
transaction boundaries, observability. This is why fixture-first adapter
development (above) and the M8a/M8b tiered-autonomy chassis
(`Tier.PROPOSE`/`NOTIFY`/`AUTO`) exist — they are the engineering controls
that make broad delegated authority safe, not a substitute for it.

**Financial governance:**

- Current autonomous operating budget: **$20/month**
  (`config/defaults/ops.json` → `autonomy.monthly_spend_cap_usd`). Hard
  ceiling unless the operator explicitly changes it — this is the
  Budget-expansion boundary above, never crossed unilaterally.
- Etsy Ads (or any paid-advertising spend) is **not** currently authorized
  from that $20 — do not assume it is. The $20 is for the currently
  authorized expense categories only (listing renewal fees, POD SKU base
  cost at Gate-3-approval time, planner LLM tokens).
- Freely allocate the existing $20 among currently authorized expenses
  without asking.
- Recommend a separate advertising budget once there's enough evidence
  (from the Pinterest/social experiment loop, see
  `docs/designs/2026-08-24-pinterest-adapter-and-loop-roadmap.md`) to
  justify one — that recommendation itself crosses the budget-expansion
  boundary, so it's a proposal to the operator, not a unilateral spend.

**Objective — optimize for contribution profit and long-term portfolio
value, not gross sales or activity.** Trailing-window contribution profit
(revenue minus Etsy fees, fulfillment cost, and attributed ad spend) is the
primary metric, alongside the expected long-term value of the product
portfolio, subject to the capital-at-risk ceiling above. This is a
deliberate correction against "maximize sales," which would treat an ad
that turns $30 into $35 of revenue on a $20-cost canvas as a win. See
`docs/research/2026-08-24-etsy-path-to-profitability.md` for the current
ground-truth numbers this objective is being applied against.

**Reporting stays act-then-show, not ask-then-act**, for every decision
inside the delegated scope above: execute, record what was done and why,
measure the result, adjust. Diffs/changes are shown to the operator for
visibility after the fact, not held for approval before.

## Current focus

**Amended 2026-08-09.** The app pivoted: automated Etsy gating (scoring,
viability, Gate 1 curation) is removed. Active work is the mass-mode **RAW
auto-edit engine** — `shopsteward edit run <folder> --look <name|description>`
writes Adobe Camera Raw `.xmp` sidecars (a conservative correction pass plus
a described "look") next to each RAW for Lightroom Classic to read on
import. Design: `docs/superpowers/specs/2026-08-09-raw-auto-edit-engine-design.md`;
plan: `docs/superpowers/plans/2026-08-09-raw-auto-edit-engine.md`. Hero-mode
Etsy shop-building is deferred to a later effort sourced from a manually
curated winners folder, not automated scoring.

The milestone table (`docs/PRD_v2.1.md` §10) reflects this: M3–M5
(hero-mode scoring/curation, mockups, listing creation) are superseded.
Check the milestone table and §4.3/§10 amendments before starting new work.
The PRD wins on any disagreement with this file — flag the discrepancy.

All 16 open questions from `KICKOFF.md` §2 were resolved on 2026-07-03 and
are folded into PRD v2.1 (see its §13 for the decision log). No stage is
blocked on open questions.

**Amended 2026-08-24.** The "shop-building is deferred" language above is
now stale and superseded. The operator directed active autonomous
shop-management of the real, live Etsy shop (52644245) as of this date —
see `docs/research/2026-08-24-etsy-path-to-profitability.md` for the
ground-truth business plan and `docs/designs/` (Pinterest adapter +
`social.pinterest_post`) for the active build. This is not sourced from a
"manual winners folder" precondition; it operates on the shop's existing 27
listings directly. The operator has also directed the autonomy chassis
toward *more* autonomy, not less, subject to two fixed, non-negotiable
limits regardless of trust earned: (1) capabilities whose `max_tier` is
hard-pinned to `Tier.PROPOSE` in Python (not config) stay operator-approved
— this reflects a stated platform irreversibility (e.g. `seo_edit`: a bad
title/tag edit resets Etsy's search-ranking history with no API to restore
it; `gapfill_reprint`: creates a real paid POD SKU), not a trust judgment,
and does not lift as the agent proves itself; (2) the $20/month autonomy
spend ceiling (`config/defaults/ops.json` `autonomy.monthly_spend_cap_usd`)
is the operator's own budget decision and is never raised unilaterally.
Everything else — ladder promotion speed, which capabilities default to
`Tier.NOTIFY` for a *newly designed* capability whose risk profile
genuinely supports it (e.g. Pinterest posting: free, individually
deletable, non-search-ranking-affecting), platform/channel coverage — is a
live autonomy dial, not a hard wall, and defaults toward more autonomy
going forward.

**Superseded, same day (2026-08-24).** The paragraph above described a
narrower, capability-by-capability widening. It has been superseded by a
full governance rewrite — see "Governance & decision authority" above,
explicit operator authorization. The `Tier.PROPOSE`-hard-pin point still
holds (a Python-level tier ceiling reflecting platform irreversibility,
e.g. `seo_edit`), but it is now one instance of the general
Destructive-data/Truly-irreversible boundaries in that section, not a
standalone rule. The $20/month ceiling statement is superseded by the
Financial governance subsection above (same number, now with the explicit
Etsy-Ads carve-out and reallocation authority spelled out).
