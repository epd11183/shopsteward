# ShopSteward — Product Requirements Document (v2.1)

*Owner: Eric DiPietro · Status: Draft · Date: July 3, 2026 · Supersedes: PRD v2 (July 1, 2026)*

> PRD v2 is preserved at `docs/PRD_v2.md` for history. This document is the
> current spec. Where they conflict, v2.1 wins.

## 1. Overview

ShopSteward is a modular workflow tool for a photography business. Its **editing
module** ingests mixed RAW + JPEG folders and produces consistently edited
photographs — usable on its own for event work (weddings, races, sports,
brewery events) with no coupling to any marketplace. Its **Etsy pipeline**
picks up a curated subset of those photos from a landing folder and takes them
the rest of the way: viability scoring, mockup generation against a staging
template library, listing creation across digital downloads and physical
products (Printful + Gelato), Instagram promotion, and a performance feedback
loop into tuning profiles.

The operator touches the pipeline at exactly three lightweight gates in the
hero/Etsy path — curate, finish, publish — and everything between runs
unattended. Mass mode (event work) has its own lighter-weight flow described
in §4.2. ShopSteward is a local-first tool developed publicly on GitHub from
day one, built first to drive sales for PhotosByEricD.

### 1.1 Changes from v2

- **Folder-pointed ingestion.** Discovery is invoked by pointing the tool at
  a folder containing paired RAW + JPEG files (`shopsteward ingest <path>`),
  not by a background watcher on a static path. The ingester pairs files by
  base filename, scores against the JPEG, and carries the RAW path forward.
- **Two editing modes: hero and mass.** Hero mode preserves the PRD v2 flow
  (score, curate, per-subject preset). Mass mode is optimized for event work
  — batch preset application, no per-image commercial scoring, gallery-style
  output — and is the standalone use case for event photography.
- **The editing module is a standalone deliverable.** It runs without any
  Etsy, POD, or Instagram code loaded, so it can serve event work
  independently. The Etsy pipeline consumes editing output through a landing
  folder handoff (§1.4), not through in-process function calls.
- **Landing-folder handoff.** After editing, photos flagged for Etsy are
  placed in a configured landing folder. The Etsy pipeline watches only this
  folder — a single, well-defined interface between the editing module and
  the rest of the system.
- **Staging template library as a first-class deliverable.** Stage 3 mockup
  generation composites against a curated library of room templates (multiple
  rooms, décor styles, lighting) with marked blank regions. This is now an
  explicit deliverable — not just a runtime prompt to an image model.
- **AI authorization is explicit.** Vision models are authorized for hero
  identification / commercial scoring, Instagram caption + hashtag
  generation, Etsy title / tag / description generation, and empty-room scene
  generation for the template library. AI never touches the photograph
  itself. Provider selection per use case is a discussion, not an assumption.
- **Development workflow formalized.** All non-trivial implementation runs in
  Claude Code sub-agents with a reviewer sub-agent gating output before it
  reaches the operator. Every major decision goes through explicit operator
  review. Major designs get a C-Suite critique panel before finalization.
  See §8.

### 1.2 Changes from v1 (historical)

See `docs/PRD_v2.md` §1.1 for the v1 → v2 changes (Printify dropped, POD-first
Stage 4, three-gate framing, vision scoring, public-repo posture).

## 2. Problem and Opportunity

Unchanged from v2 in substance. Running a wildlife and landscape photography
Etsy shop plus event work requires repetitive manual work across disconnected
tools. A recent manual shop refresh plus consistent posting produced a
measurable sales bump, validating that catalog freshness and posting cadence
are the highest-leverage activities. ShopSteward makes both continuous and
data-driven rather than episodic — and separately delivers value on event work
that never touches Etsy.

## 3. Goals and Non-Goals

### 3.1 Goals (v1 release)

- Operator involvement in the hero/Etsy path limited to three gates; per-hero
  operator time under 10 minutes end to end (finishing pass 3–5 min of that).
- Mass-mode event workflow reduces per-event editing time by at least 60%
  versus the current manual process.
- The editing module is installable and usable standalone with no Etsy stack
  imported.
- Reduce per-listing manual effort by at least 75% versus the current manual
  process.
- Enforce an Instagram cadence of at least 4 posts per week with no more than
  one operator tap per post.
- Generate AI-composited mockup scenes tuned to each image; the artwork
  itself is never AI-generated or AI-altered.
- Single source of truth for catalog, listings, fulfillment routing, and
  performance across Etsy, Printful, Gelato, and Instagram.
- Close a measurable feedback loop: curation weights, edit preset families,
  mockup styles, and format mixes adjust from rolling sales and engagement
  data.
- Ship as a clean, documented, licensable open-source project a stranger can
  clone and run against their own shop.

### 3.2 Non-Goals (v1 release)

- Full automation of artistic edit decisions — the finishing pass is
  deliberate and stays for hero mode.
- Multi-tenant SaaS deployment. The schema keeps `user_id` foreign keys, but
  v1 ships single-operator and local-first.
- Replacing Lightroom Classic or its catalog. Integration is via the extended
  EPD Edit Bridge plugin.
- Order management, customer service, or shipping — Etsy and the POD
  providers own these.
- Channels beyond Etsy and Instagram (Pinterest, Facebook, TikTok deferred to
  v2).
- A gallery delivery / client-proofing product. Mass mode's output stops at
  edited exports; how those get delivered to the client is out of scope for
  v1 (though the exports should be delivery-ready).

## 4. Operator Touchpoints

### 4.1 Hero mode (feeds the Etsy pipeline): three gates

Unchanged from PRD v2 in structure. Ingestion is now folder-pointed.

| Gate | Where | What the operator does | Target time |
|---|---|---|---|
| **1 — Curate** | ShopSteward web UI (local) | Review scored candidates in a swipe/keyboard batch UI: approve, reject, snooze. Score rationale shown per image. | ~5 sec/image |
| **2 — Finish** | Lightroom Classic | Approved heroes arrive in a "ShopSteward — Needs Finishing" collection with the subject-appropriate preset family already applied via EPD Edit Bridge. Operator does a quick finishing pass and exports to the landing folder. Export is the approval signal for Etsy pickup. | 3–5 min/hero |
| **3 — Publish** | ShopSteward web UI / mobile | Review the drafted listing set per hero (copy, mockups, formats, pricing, provider routing) and the queued Instagram assets. One tap publishes. | ~1 min/hero |

### 4.2 Mass mode (event work, standalone)

Mass mode has one active touchpoint plus optional post-hoc curation:

| Touchpoint | Where | What the operator does | Target time |
|---|---|---|---|
| **Invocation** | Terminal or UI | Point ShopSteward at the folder; select or confirm event preset family (wedding, half-marathon, brewery event). System applies presets across the batch via EPD Edit Bridge, exports gallery-ready JPEGs to the configured output folder. | Seconds to invoke |
| **Optional review** | Lightroom Classic | Standard event finishing pass on the batch. No per-image scoring, no curation UI. | Normal event edit time |
| **Optional Etsy send-off** | ShopSteward CLI or UI | Flag any post-event standouts to the Etsy landing folder for later processing by the hero pipeline. | Seconds |

## 5. Functional Scope by Stage

### 5.1 Stage 1: Discovery (folder-pointed) and Commercial Viability Scoring

- **Invocation.** Primary surface: the local web UI's folder picker with a
  hero/mass mode toggle. Underlying, scriptable path:
  `shopsteward ingest <path> --mode {hero,mass}`. Both ship with M2. The
  path is a folder containing mixed RAW (`.CR3`) and JPEG previews. The
  ingester pairs files by base filename (`IMG_1234.CR3` + `IMG_1234.JPG`),
  records content hashes on the RAW to prevent duplicates, and extracts EXIF
  from the paired JPEG via Pillow (capture metadata is identical to the
  RAW's; avoids a CR3-parsing dependency — amended 2026-07-03). The RAW is
  hash-tracked and carried forward as the print master.
- **Mass-mode preset selection.** The event preset family is chosen at
  invocation (`--preset <family>`, or the UI equivalent); if omitted, the
  tool lists available families and asks for confirmation. No folder-name
  convention or EXIF inference.
- **Hero mode scoring** (unchanged from v2 in content):
  - Technical score: resolution headroom, sharpness (Laplacian variance on
    subject region), exposure distribution, noise estimate.
  - Commercial score: vision-model rating on décor fit, subject appeal,
    print-worthiness at 24×36+, palette marketability. Returns structured
    score + one-line rationale surfaced at Gate 1.
  - Catalog gap score: rewards underrepresented subjects/palettes/orientations.
  - Historical conversion score: subject-category conversion rates; weight
    ramps up as sales data accumulates.
  - Composite score with tuning-profile weights; above-threshold candidates
    queue for Gate 1.
- **Mass mode scoring:** technical only (blur, exposure, blinks/eyes-closed
  where detectable). No commercial or catalog scoring. Purpose is to flag
  rejects, not to rank.

### 5.2 Stage 2: Editing (modular, dual-mode)

The editing module lives at `src/shopsteward/editing/` and imports nothing
from the Etsy/POD/IG stack. It ships in the same package as the rest of
ShopSteward with its own console entry point (`shopsteward edit`); an
import-linter rule in CI enforces the module boundary from M2 onward. No
separate PyPI package in v1 — split later only if outside demand justifies
the release overhead.

- **Job dispatch.** On operator approval (hero mode) or invocation (mass
  mode), ShopSteward writes a job file (photo IDs + preset family) to the
  jobs folder polled by the extended EPD Edit Bridge.
- **EPD Edit Bridge extension.** The plugin's two manual commands are joined
  by a background queue processor task that polls the jobs folder while LrC
  is open, applies preset families as develop settings (one undoable history
  step each, as today), and adds photos to a mode-specific collection
  ("ShopSteward — Needs Finishing" for hero; the event's own collection for
  mass). In mass mode the plugin then auto-exports gallery-ready JPEGs via
  an export session to the configured event output folder using a per-event
  naming template (decided 2026-07-03); the queue processor's write
  authorization is granted per-session by the operator when it starts.
- **Preset families** are named develop-settings dicts stored in the
  database, seeded from `config/defaults/preset_families/` (JSON), never
  hardcoded. v1 ships neutral placeholders; the operator captures real
  looks with the Bridge's export command.
- **Hero-mode finishing.** Operator's Gate 2 quick pass in LrC; export to
  the landing folder using a provided export preset (AdobeRGB TIFF print
  master + sRGB JPEG web derivative, deterministic naming).
- **Mass-mode finishing.** Optional standard-event edit pass. Output is
  both the event's LrC collection (created by the Bridge for the finishing
  pass) and delivery-ready JPEGs exported to the configured event output
  folder. Client gallery / delivery is out of scope for v1 (§3.2).
- **Landing folder handoff.** The Etsy pipeline watches only the landing
  folder. This is the sole *image/artifact* handoff between the editing
  module and the rest of ShopSteward; commands flow one-way from the
  pipeline into editing's dispatch (never the reverse — CI-enforced).
  (Wording amended 2026-07-03.) Files arrive by either path: the Gate 2 export preset
  writes there directly, or the operator drags files in manually (mass-mode
  standouts, catalog backfill). Anything in the landing folder is
  commercially approved by definition — the pipeline never re-scores it,
  validating only technical properties (resolution, color space, file
  integrity).

### 5.3 Stage 3: Mockup Generation

- **Staging template library.** ~15 AI-generated empty-room templates at
  v1 launch (≈3 orientations × 5 room/style combos), shipped with the repo
  in `config/defaults/` and extensible per-operator. Templates are
  generated offline with consumer image tools (Gemini / ChatGPT image
  generation) — many candidates generated, the best hand-picked by the
  operator with Claude-assisted annotation tooling. No hand-curated room
  photography in v1. Each template is the room image plus a JSON sidecar
  containing the blank display region as four-corner quad coordinates
  (feeding OpenCV's perspective transform directly) and tags (room type,
  style, lighting, orientation).
- **Compositing.** For each approved Variant, ShopSteward selects templates
  matching the image's palette / mood / orientation and composites the print
  into the marked region with Pillow/OpenCV (perspective and lighting
  blends). The photograph itself is never AI-touched.
- **AI template expansion.** Post-launch, coverage gaps are filled the same
  way — new rooms generated offline, annotated, added to the library ahead
  of time, never per-listing at runtime. No runtime scene-generation
  adapter is built in v1; the adapter interface remains a defined seam so a
  programmatic generator (Imagen, Flux via Replicate) can slot in later.
  Compositing stays deterministic and costs amortized.
- **Templates by intent.** Single-image, gallery-wall bundle, canvas
  edge-on, framed-poster room scene (Gelato SKUs), acrylic depth shot,
  digital "what you get" graphics.

### 5.4 Stage 4: Listing Creation (POD-first for physical SKUs)

Unchanged from PRD v2 in mechanics. Source of print files is now the landing
folder, not a legacy staging directory.

- Variation matrix per Variant across digital and physical SKUs; format
  selection driven by tuning profile.
- Gelato: template-based Create Product pushes drafts to the connected Etsy
  shop with fulfillment linkage.
- Printful: equivalent path via sync-product API.
- Enrichment pass updates the Etsy draft (title, tags, description, mockup
  images, price) via the Etsy API. Never modify SKU values or variation
  structure.
- Digital listings: created directly via the Etsy API.
- Bundle proposer clusters the catalog and proposes bundles for Gate 3.
- All listings land as Etsy drafts and wait at Gate 3.

### 5.5 Stage 5: Listing Management

Unchanged from PRD v2.

### 5.6 Stage 6: Marketing (Instagram)

Unchanged from PRD v2. Instagram stays with the Etsy pipeline — it is Etsy
promotion, not an editing-module concern.

## 6. AI Feedback Loop

Unchanged from PRD v2.

## 7. Architecture

### 7.1 Pattern and stack

Monolithic Python core owning the data model and orchestration, with external
systems behind pluggable adapter interfaces. Backend Python 3.12 + FastAPI;
SQLite with event-sourced schema; React + Vite frontend served locally;
Pillow/OpenCV for compositing; adapter layers for scene generation, vision
scoring, Etsy, Printful, Gelato, Instagram; Lightroom via the extended EPD
Edit Bridge plugin.

**Module boundaries (new in v2.1):**

- `src/shopsteward/editing/` — standalone editing engine. Imports:
  `adapters/lightroom` (EPD Edit Bridge job dispatch), image tooling, tuning
  profiles it owns. Does not import from `adapters/etsy`, `adapters/printful`,
  `adapters/gelato`, `adapters/instagram`, or the listing/marketing modules.
- `src/shopsteward/pipeline/` — Etsy pipeline (Stages 3–6). Consumes edited
  photos only via the landing-folder watcher.
- `src/shopsteward/core/` — data model, event store, projections, shared
  services used by both.

### 7.2 Data model (core entities)

Unchanged from PRD v2, plus:

| Entity | Description |
|---|---|
| **IngestJob** | A folder-pointed ingestion invocation. Records path, mode (hero/mass), event preset (if mass), and links to the Photos it produced. |
| **StagingTemplate** | A room-scene template: image path, mask/coordinates for the blank region, tags (room type, style, lighting, orientation). |

### 7.3 Multi-tenant readiness

Unchanged from PRD v2.

## 8. Development Workflow (new in v2.1)

The following are load-bearing project rules, not preferences.

### 8.1 Sub-agent implementation, main-thread orchestration

All non-trivial implementation runs in Claude Code sub-agents, defined under
`.claude/agents/`. The main session orchestrates, presents diffs, and gates
decisions to the operator. Standard roster:

- **`architect`** — designs, ADRs, adapter interfaces. Read-only until a
  direction is approved.
- **`python-impl`** — Python implementation within an approved design.
  Constrained to `src/`, `tests/`, `config/`.
- **`test-author`** — pytest tests against recorded fixtures. Never hits
  live APIs.
- **`reviewer`** — reviews sub-agent output against CLAUDE.md guardrails and
  the current PRD milestone before it reaches the operator.
- **`lua-impl`** — Lightroom plugin work. Constrained to
  `plugins/epd-edit-bridge/`.

### 8.2 Operator review on every major decision

The following require explicit operator approval before merge or wiring:

- Architecture or adapter-interface changes
- Amendments to CLAUDE.md, the PRD, or `.claude/settings.json`
- New dependencies
- New external services or API providers
- AI model / provider selection
- Anything touching secrets or the credentials boundary
- The first PR in each milestone

### 8.3 C-Suite critique for major designs

Before finalizing a major design, the main session runs a C-Suite critique
panel (CTO / CFO / CMO / CPO / Chief Legal), each voice 2–4 sentences with
at least one concrete improvement proposal. Transcripts surface to the
operator as review artifacts.

### 8.4 No live APIs by default

No calls to Etsy, Printful, Gelato, Instagram, or AI providers from any code
path until adapter design + fixtures + smoke-test plan are approved for that
provider.

### 8.5 AI providers, model policy, and budget (resolved 2026-07-03)

- **Development workflow** (Claude Code sessions, sub-agents, C-Suite
  critiques) runs on the operator's Claude Max plan — no marginal API
  spend.
- **Runtime adapters default to the Gemini API** (operator already has
  access): Gemini Flash triages the commercial score for every candidate;
  Gemini Pro re-scores candidates near the Gate 1 threshold. Copy
  generation (Etsy titles / tags / descriptions, IG captions) uses one
  Gemini model across all surfaces, driven by a house voice/style guide in
  `config/defaults/` injected into every prompt. The style guide is
  authored before the first generated listing.
- **Budget:** realistic runtime spend $0–5/mo on the Gemini free tier;
  soft cap $10/mo, spend logged in the database with an alert at 80%.
  Anthropic-API and OpenRouter adapters are future options behind the same
  provider-agnostic interface.
- **Sub-agent model policy:** tiered — top-tier model for `architect` and
  `reviewer`; faster/cheaper models for `python-impl`, `test-author`, and
  `lua-impl`.
- **C-Suite critique cadence:** automatic at every milestone kickoff and
  for any §8.2-class decision; ad hoc on operator request.

## 9. External Integrations

Unchanged from PRD v2.

## 10. Build Order

Re-sequenced from PRD v2 to reflect the standalone editing module. The
editing module can deliver value to event work before any Etsy code exists.

| Milestone | Scope | Estimate | Outcome |
|---|---|---|---|
| **M0** | Public repo scaffold + amendments folded in + sub-agent roster + Workiva addendum confirmed (✅ confirmed 2026-07-03). | 1 evening | Safe to build in the open. |
| **M1** | Etsy data pull + analytics dashboard (data model foundation). | 1 weekend | Shop performance visible; baseline established. |
| **M2** | Editing module — folder-pointed ingestion, RAW+JPEG pairing, EPD Edit Bridge queue processor, mass-mode preset application, standalone CLI. | 2 weekends | Event work runs on ShopSteward independently. |
| **M3** | Hero-mode viability scoring + Gate 1 curation UI + landing-folder handoff. | 2 weekends | Etsy pipeline picks up where editing leaves off. |
| **M4** | Staging template library + mockup compositor + AI template expansion. | 2–3 weekends | Largest manual-effort reduction lands. |
| **M5** | Listing drafts: Gelato + Printful adapters, Etsy enrichment, Gate 3 UI. | 2–3 weekends | Listing time cut to minutes. |
| **M6** | Instagram asset generation + scheduled posting. | 1 weekend | Cadence on autopilot. |
| **M7** | Feedback loop v1: tuning profiles + weekly action queue. | 2 weekends | System becomes self-improving. |

## 11. Success Metrics

Unchanged from PRD v2, plus:

- Mass-mode per-event editing time reduction ≥60% vs. current manual
  process.
- Editing module standalone: `pip install shopsteward-edit` (or equivalent
  boundary) produces a working install with zero Etsy/POD/IG credentials.

## 12. Risks and Mitigations

Unchanged from PRD v2 in substance. One addition:

- **Editing module boundary erosion.** Convenience imports over time will
  couple editing back to the Etsy pipeline. Mitigation: import-linter rule
  in CI enforcing the boundary from M2 onward.

## 13. Open Questions

All 16 kickoff questions (`KICKOFF.md` §2) were resolved on July 3, 2026 and
folded into the sections above (§4.2, §5.1, §5.2, §5.3, §8.5, §10). No open
questions remain. Decision log summary:

1. Mode selection: UI toggle primary, CLI `--mode` kept (§5.1)
2. Mass preset family: invocation flag with confirm (§5.1)
3. Mass output: LrC collection + exported JPEGs; delivery out of scope (§5.2)
4. Packaging: one package, `shopsteward edit` entry point (§5.2)
5. Landing folder: export preset + manual drag both valid (§5.2)
6. No re-scoring from the landing folder; technical validation only (§5.2)
7. Templates: AI-generated rooms only (§5.3)
8. Region marking: JSON sidecar, four-corner quads + tags (§5.3)
9. Library size: ~15 at launch, operator + Claude-assisted tooling (§5.3)
10. Vision scoring: Gemini Flash triage + Gemini Pro borderline (§8.5)
11. Room generation: offline via consumer tools; no runtime adapter in v1 (§5.3)
12. Copy: one model + house style guide authored first (§8.5)
13. Sub-agent models: tiered (§8.5)
14. C-Suite cadence: milestone kickoffs + §8.2 decisions (§8.5)
15. Budget: Max plan for dev; Gemini free tier for runtime; $10/mo soft cap (§8.5)
16. Workiva addendum: confirmed enumerated; public repo cleared (§10)

M2 kickoff decisions (2026-07-03):

17. EXIF read from paired JPEG via Pillow, not the CR3; RAW is hash-only (§5.1)
18. Mass-mode export: plugin auto-exports via export session, per-event naming template (§5.2)
19. import-linter approved as dev dependency; boundary rule lands in the first M2 PR
20. Preset families: DB-stored, seeded from config/defaults/preset_families/ (§5.2)

M3 kickoff decisions (2026-07-03):

21. Composite score 0–100 over registered scorers (technical + commercial live;
    catalog-gap and historical-conversion registered at weight 0 until M5 data
    exists). Default Gate 1 queue threshold 60; Flash scores within ±10
    escalate to Gemini Pro. All tunable in the DB tuning profile.
22. Vision input: paired JPEG downscaled to 1024px long edge, ALL EXIF
    (incl. GPS) stripped before upload. Vision adapter uses httpx against the
    Gemini REST API directly — no vendor SDK dependency. Structured verdict
    (scores + subject + strongest_room_style + one_risk + one-line rationale).
    LLM spend logged as events from the first call.
23. Gate 1 UI: keyboard-first single card (A/R/S + Z undo, auto-advance,
    preload, snoozed shelf, inline dispatch state).
24. Landing folder: on-demand scan (CLI/API/UI-poll triggered), no daemon.
    Technical validation only (resolution, color space, integrity) — never
    re-scored (decision 6).
25. Hero preset selection: `hero_preset_family` key in the tuning profile
    (seeded "neutral"); subject-mapped selection deferred until real
    subject-specific families exist.
26. Gate 1 snooze: indefinite visible shelf, manual re-queue only.
27. §5.2 landing-folder wording amended: sole *image* handoff; commands flow
    pipeline→editing.dispatch one-way (import-linter enforced).

M4 kickoff decisions (2026-07-04):

28. Development against synthetic placeholder templates (programmatically
    drawn rooms with known quads, committed as dev defaults); the real ~15
    AI-generated rooms land later as a content-only PR (annotate + commit).
29. Compositing v1 realism: perspective warp + ambient brightness/WB match +
    soft edge shadow + intent-appropriate frame/wrap border. Deterministic,
    no AI, no reflections.
30. All six mockup intents ship in M4: single, gallery-wall bundle (multi-
    region templates), canvas edge-on, framed-poster room scene, acrylic
    depth shot, digital "what you get" graphic.
31. Sidecar schema includes `region_width_inches` for believable print
    scale, computed not eyeballed. Etsy AI-imagery disclosure line templated
    now for M5 listings; template annotation via a Templates UI surface
    (click four corners).

M4 design resolutions (2026-07-04):

32. Gallery-wall v1 fills extra regions with varied deterministic crops of
    the one photo (mockup presentation only; companion-photo selection
    arrives with M5 catalog data).
33. Manual-drop landing files (photo_id unknown) get full mockup sets —
    this is the catalog-backfill path.
34. Deterministic mockup compositing (incl. the acrylic gloss overlay) is
    compatible with "AI never touches the photograph": the rule targets
    generative edits and the sold file.

Etsy auth design (2026-07-04, from Open API v3 docs verification):

35. Etsy access tokens live 1 hour and refresh tokens ROTATE on every use
    (90-day life). Therefore: an `EtsyTokenStore` persists tokens in
    `data/etsy_tokens.json` (gitignored, agent-read-denied) with automatic
    refresh + immediate rotated-token persistence; a one-time
    `shopsteward etsy auth` CLI command runs the authorization-code + PKCE
    consent flow via a localhost redirect (callback
    `http://localhost:8322/oauth/redirect`) and auto-discovers the shop id;
    scopes are read-only (`listings_r transactions_r shops_r`) until M5
    re-consents for write. Only `ETSY_API_KEY` stays in `.env`. Hard rule:
    **tokens never enter the event log** (append-only = undeletable).
    Operational notes: keystring inactive until Etsy approves registration;
    personal access suffices for own-shop use (no commercial access);
    dormant apps (6 months without a request) are banned.

## 14. Appendix: Deferred to v2+

Unchanged from PRD v2.
