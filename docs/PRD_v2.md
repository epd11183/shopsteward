# ShopSteward — Product Requirements Document (v2)


*Owner: Eric DiPietro · Status: Draft · Date: July 1, 2026 · Supersedes:
PRD v1 (June 10, 2026)*

## 1. Overview

ShopSteward is an end-to-end workflow tool for running a print-on-demand
and digital download photography business on Etsy. It automates the
pipeline from hero identification through consistent editing, mockup
generation, Etsy listing creation (digital downloads plus physical
products fulfilled by Printful and Gelato), and Instagram promotion —
and closes a data feedback loop from Etsy and Instagram performance back
into curation scoring, editing presets, mockup styles, and format-mix
decisions.

v2 sharpens the operating principle established in v1: the operator
touches the pipeline at exactly three lightweight gates — curate,
finish, publish — and everything between runs unattended. It also
commits to building in the open: ShopSteward is a local-first tool
developed publicly on GitHub from day one, built first to drive sales
for PhotosByEricD.

### 1.1 Changes from v1

- **POD lineup:** Printify is dropped. Physical fulfillment routes to
  Printful (canvas, acrylic, metal) and Gelato (framed posters, fine art
  paper prints, international orders via its local-production network).

- **Stage 4 inverted:** For physical SKUs, listings are born on the POD
  side, not the Etsy side. Provider store-product APIs (Gelato's
  template-based Create Product API; Printful's sync-product API) push
  drafts to the connected Etsy shop with fulfillment linkage intact;
  ShopSteward then enriches the draft (title, tags, description, mockup
  images) via the Etsy API. Digital listings are created directly via
  the Etsy API. This removes the fragile SKU-relinking step that v1's
  Etsy-first flow would have required.

- **Operator touchpoints formalized:** Three gates replace the loosely
  defined review steps of v1: Gate 1 Curate (batch approve/reject scored
  candidates), Gate 2 Finish (a quick Lightroom finishing pass on each
  approved hero), Gate 3 Publish (one-tap review of drafted listings and
  queued Instagram posts). Target operator time: under 10 minutes per
  hero end to end.

- **Commercial viability scoring gets teeth:** Stage 1 adds a
  vision-model commercial score (décor fit, subject appeal,
  print-worthiness at large sizes) alongside technical quality, catalog
  gap, and historical conversion. Scoring weights live in the
  TuningProfile.

- **Public on GitHub from day one:** v1's "private during build" posture
  is reversed. The repository is public from the first commit, which
  reshapes credential handling, configuration/data separation,
  licensing, and documentation (Section 8).

- **Confirmed in scope:** Instagram (Stage 6) remains in scope for v1,
  unchanged in intent from PRD v1.

## 2. Problem and Opportunity

Unchanged from v1 in substance. Running a wildlife and landscape
photography Etsy shop requires repetitive manual work across
disconnected tools: candidate selection in Lightroom, edit finishing,
mockup generation, listing creation, fulfillment configuration, and
Instagram posting. A recent manual shop refresh plus consistent posting
produced a measurable sales bump, validating that catalog freshness and
posting cadence are the highest-leverage activities. ShopSteward makes
both continuous and data-driven rather than episodic.

## 3. Goals and Non-Goals

### 3.1 Goals (v1 release)

- Operator involvement limited to the three gates; per-hero operator
  time under 10 minutes from ingestion to published listing (excluding
  LrC finishing, which is capped at a "quick pass" — target 3–5 minutes
  per hero).

- Reduce per-listing manual effort by at least 75% versus the current
  manual process.

- Enforce an Instagram cadence of at least 4 posts per week with no more
  than one operator tap per post.

- Generate AI-composited mockup scenes tuned to each image; the artwork
  itself is never AI-generated or AI-altered.

- Single source of truth for catalog, listings, fulfillment routing, and
  performance across Etsy, Printful, Gelato, and Instagram.

- Close a measurable feedback loop: curation weights, edit preset
  families, mockup styles, and format mixes adjust from rolling sales
  and engagement data.

- Ship as a clean, documented, licensable open-source project a stranger
  can clone and run against their own shop.

### 3.2 Non-Goals (v1 release)

- Full automation of artistic edit decisions — the finishing pass is
  deliberate and stays.

- Multi-tenant SaaS deployment. The schema keeps user_id foreign keys,
  but v1 ships single-operator and local-first.

- Replacing Lightroom Classic or its catalog. Integration is via the EPD
  Edit Bridge plugin (extended).

- Order management, customer service, or shipping — Etsy and the POD
  providers own these.

- Channels beyond Etsy and Instagram (Pinterest, Facebook, TikTok
  deferred to v2).

## 4. Operator Touchpoints: The Three Gates

Everything not listed below is automated. Each gate is designed for
mobile or a quick desktop session.

| **Gate**         | **Where**                   | **What the operator does**                                                                                                                                                                                                                                                                  | **Target time** |
|------------------|-----------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------|
| Gate 1 — Curate  | ShopSteward web UI (local)  | Review scored candidates in a swipe/keyboard batch UI: approve, reject, or snooze. Score rationale (technical, commercial, gap, history) shown per image. Auto-approve threshold configurable once trust is earned.                                                                         | ~5 sec/image    |
| Gate 2 — Finish  | Lightroom Classic           | Approved heroes arrive in a "ShopSteward — Needs Finishing" collection with the subject-appropriate preset family already applied via EPD Edit Bridge. Operator does a quick finishing pass (WB, local adjustments, crop) and exports to the staging folder. Export is the approval signal. | 3–5 min/hero    |
| Gate 3 — Publish | ShopSteward web UI / mobile | Review the drafted listing set per hero (copy, mockups, formats, pricing, provider routing) and the queued Instagram assets. One tap publishes Etsy drafts; one tap approves IG posts into the scheduler.                                                                                   | ~1 min/hero     |

Everything between the gates runs unattended: ingestion, scoring, preset
application, mockup generation, listing drafting, POD product
registration, copy generation, IG asset generation, performance pulls,
and tuning.

## 5. Functional Scope by Stage

### 5.1 Stage 1: Discovery and Commercial Viability Scoring

- Watch a configured Lightroom export folder (candidate exports, e.g.,
  2048px sRGB previews); ingest by content hash to prevent duplicates.
  Extract EXIF: camera, lens, focal length, capture time, GPS where
  present.

- Technical score: resolution headroom for large prints, sharpness
  (Laplacian variance on subject region), exposure distribution, noise
  estimate from ISO plus shadow analysis.

- Commercial score (new): a vision-model adapter (Claude API vision as
  the initial implementation; interface allows a local model later)
  rates each candidate on décor fit (would this hang in a living room,
  office, cabin), subject appeal within the shop's niche, emotional
  impact, palette marketability, and print-worthiness at 24×36 and
  larger. Returns a structured score plus a one-line rationale surfaced
  at Gate 1.

- Catalog gap score: rewards subjects, palettes, and orientations
  underrepresented in the live catalog.

- Historical conversion score: subject-category conversion rates from
  the Performance store; weight ramps up as sales data accumulates and
  the commercial score's weight ramps down correspondingly.

- Composite score with weights from the active TuningProfile. Candidates
  above the review threshold queue for Gate 1; the rest are archived
  with scores retained (retrievable, and re-scored if the tuning profile
  changes materially).

### 5.2 Stage 2: Editing

- On Gate 1 approval, ShopSteward writes a job file (photo IDs plus
  preset family selected by subject classification and current tuning)
  to a jobs folder.

- EPD Edit Bridge is extended from its current two manual commands into
  a queue processor: while Lightroom Classic is open, a plugin
  background task polls the jobs folder, applies the preset family as
  develop settings (one undoable history step, as today), and adds the
  photos to the "ShopSteward — Needs Finishing" collection. No headless
  Lightroom automation is attempted — the LrC SDK does not support it
  reliably, and the polling model keeps the plugin simple and
  inspectable.

- Operator performs the Gate 2 finishing pass and exports approved
  heroes to the staging folder using a provided export preset:
  full-resolution AdobeRGB TIFF print master plus sRGB JPEG web
  derivative, deterministic naming.

- Staging watcher registers the export as an approved Variant and
  triggers Stage 3 automatically.

### 5.3 Stage 3: Mockup Generation

- For each approved Variant, generate a mockup pack of 8–12 scenes. AI
  generates only empty room scenes (prompted from the Variant's mood,
  palette, orientation) via a swappable adapter (Imagen and
  Flux-via-Replicate initially). Scenes are cached and reused; the
  library grows over time to hold amortized cost under \$0.50 per
  Variant.

- The actual print is composited into the empty wall zone
  deterministically with Pillow/OpenCV perspective and lighting blends.
  AI never touches the artwork.

- Templates by intent: single-image, gallery-wall bundle, canvas
  edge-on, framed-poster room scene (Gelato SKUs), acrylic depth shot,
  and digital "what you get" graphics.

- Optional lead-image experiment: real photographs of physical prints in
  the operator's home, tracked against AI mockups for conversion lift.

### 5.4 Stage 4: Listing Creation (POD-first for physical SKUs)

- Variation matrix per Variant: digital single, digital packs (5, 10),
  canvas sizes (Printful), acrylic sizes (Printful), framed poster and
  fine-art paper sizes (Gelato). Format selection driven by Stage 1
  scoring and tuning profile (panoramic landscapes favor canvas-first;
  intimate wildlife favors acrylic-first; classic compositions favor
  framed paper).

- Physical flow — Gelato: one pre-built Gelato template per product
  family (created once in the Gelato dashboard); ShopSteward calls Get
  Template, substitutes the Variant's print file into the image
  placeholders, and calls Create Product. Gelato publishes the product
  to the connected Etsy shop as a draft with variant/SKU linkage already
  wired for fulfillment.

- Physical flow — Printful: equivalent path via the Printful
  sync-product API against the connected Etsy store.

- Enrichment pass: once the draft exists on Etsy, ShopSteward updates it
  via the Etsy Open API v3 — generated title, rotated tag pool,
  EXIF-storytelling description, mockup images in tuned order, and
  pricing from the pricing rules table. SKU values set by the providers
  are never modified (changing them breaks fulfillment linkage).

- Digital flow: listings created directly via the Etsy API with the
  digital files attached; no POD involvement.

- Bundle proposer: clusters the catalog by location, subject, palette,
  or orientation and proposes bundles with projected AOV; proposals
  surface at Gate 3.

- All listings land as Etsy drafts and wait at Gate 3 for one-tap
  publish.

### 5.5 Stage 5: Listing Management

- Nightly Etsy pull: views, favorites, sales, search terms, revenue by
  variation, time-to-first-sale, stored as time-series Performance
  facts.

- Weekly scoring run buckets every active listing: performing,
  refresh-mockup, refresh-title, refresh-tags, archive, repurpose.
  Refresh actions are prepared automatically and wait at Gate 3.

- Catalog-level insights: subject and format mix performance,
  seasonality, cross-format cannibalization detection, and per-provider
  quality signals (review text correlated to fulfillment provider).

### 5.6 Stage 6: Marketing (Instagram)

- On Variant approval, auto-generate the IG asset pack: 1:1 feed image,
  9:16 reel cover, 9:16 story, 4-image detail-crop carousel.

- Caption skeleton from EXIF and Variant metadata (location, gear, time
  of day) with a short story prompt for optional operator expansion at
  Gate 3; hashtag pools by subject and location, rotated.

- Scheduling via the Instagram Graph API (Business account) with an
  enforced minimum cadence of 4 posts/week; if the queue of
  Gate-3-approved assets runs low, the next-best candidate is surfaced
  for one-tap approval. Fallback if API verification stalls:
  Buffer-mediated posting.

- Engagement signals (likes, comments, saves, reach) flow into the
  Performance store and feed Stage 1 scoring.

## 6. AI Feedback Loop

Tuning is rule-based and statistical in v1, applied through versioned,
reversible TuningProfiles read by every engine module at runtime.
Nightly aggregation rolls up Performance facts by subject, format,
mockup style, and copy template; a weekly tuning job proposes profile
updates when deltas are statistically meaningful. What gets tuned, in
addition to v1's list (preset families, mockup preferences, copy
templates, format mixes, IG asset priorities): the Stage 1 scoring
weights themselves — in particular the ramp from vision-model commercial
score toward observed historical conversion as real sales data
accumulates.

## 7. Architecture

### 7.1 Pattern and stack

Unchanged: a monolithic Python core owning the data model and
orchestration, with external systems behind pluggable adapter interfaces
(the Delphic Ledger pattern). Backend Python 3.12 + FastAPI; SQLite with
event-sourced schema (immutable events, projections for derived state);
React + Vite frontend served locally; Pillow/OpenCV for compositing;
adapter layers for scene generation (Imagen, Flux/Replicate), vision
scoring (Claude API), Etsy Open API v3, Printful API, Gelato API,
Instagram Graph API; Lightroom via the extended EPD Edit Bridge plugin.

### 7.2 Data model (core entities)

| **Entity**       | **Description**                                                                                                   |
|------------------|-------------------------------------------------------------------------------------------------------------------|
| Photo            | Source image identified by content hash. Immutable. Carries EXIF, scores, and score rationale. Has many Variants. |
| Variant          | A finished, exported version of a Photo (crop, edit treatment). Has many Renderings.                              |
| Rendering        | A Variant placed in a mockup or sized for an output (print master, IG square, gallery wall).                      |
| Listing          | An Etsy listing referencing Renderings; records fulfillment provider, provider product ID, and SKU linkage.       |
| ProviderTemplate | A registered POD blueprint (Gelato template ID or Printful product config) per product family. New in v2.         |
| Channel / Post   | Etsy, Instagram (Facebook, Pinterest v2+). Posts reference Renderings.                                            |
| Performance      | Time-series facts on Listings and Posts (views, favorites, sales, engagement).                                    |
| TuningProfile    | Versioned rule set: scoring weights, preset families, mockup styles, format mixes, copy templates, pricing rules. |

### 7.3 Multi-tenant readiness (retained, one amendment)

user_id foreign keys everywhere, configuration-over-code, and
photo-source abstraction are retained from v1. Amended: v1 specified
integration credentials encrypted in the database; because the project
is now public and single-operator, v1 ships with credentials in a
gitignored .env (documented via .env.example) with optional OS-keychain
storage. The credential store is behind an interface so the encrypted-DB
implementation can return if multi-tenant ever ships.

## 8. Building in the Open (new in v2)

- **IP prerequisite:** The repo is public from the first commit. Before
  that first push, ShopSteward must be explicitly enumerated as
  pre-existing work on the Workiva invention-assignment addendum (start
  date July 6, 2026). The public commit history then reinforces, rather
  than contradicts, the paperwork.

- **License:** Decision needed: MIT (simplest, maximum adoption) vs.
  Apache-2.0 (adds an explicit patent grant). Recommendation: MIT,
  consistent with a personal-tool-shared-generously posture.

- **Config/data separation:** No shop data, credentials, tuning history,
  or personal images in the repo. Runtime data lives in a gitignored
  data/ directory; the repo ships default tuning profiles, template
  configs, and prompt libraries that work for any wildlife/landscape
  shop out of the box.

- **Secrets hygiene:** .env.example enumerating every key (Etsy OAuth
  app, Printful, Gelato, Instagram, Anthropic/Google/Replicate); setup
  docs walk through obtaining each. Pre-commit secret scanning
  (gitleaks) in CI from day one.

- **Documentation:** README with the three-gates story and an
  architecture diagram; quickstart (clone → configure → first curation
  run); per-adapter setup guides; CONTRIBUTING.md. The EPD Edit Bridge
  plugin ships inside the repo as a subdirectory with its own install
  instructions.

- **Third-party ToS posture:** Users authenticate against their own Etsy
  developer app and POD accounts; ShopSteward never proxies anyone's
  credentials. Etsy API terms require each seller to use their own app
  registration during the personal-app tier.

## 9. External Integrations

| **System**                | **Method**                                              | **Purpose**                                                                                       |
|---------------------------|---------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| Lightroom Classic         | EPD Edit Bridge (extended: job-queue polling)           | Apply preset families, collect heroes for finishing, staging export.                              |
| Etsy                      | Etsy Open API v3 (OAuth 2.0)                            | Create digital listings; enrich POD-created drafts; publish; nightly performance pulls.           |
| Printful                  | Printful API (Etsy sync products)                       | Canvas/acrylic/metal products pushed to connected Etsy shop with fulfillment linkage.             |
| Gelato                    | Gelato API (templates + Create Product; order webhooks) | Framed poster and fine-art paper products pushed to connected Etsy shop; global local production. |
| Instagram                 | Instagram Graph API (Business account)                  | Scheduled publishing, insights. Buffer fallback if verification stalls.                           |
| Claude API                | Vision scoring adapter                                  | Commercial viability scoring with rationale.                                                      |
| Imagen / Flux (Replicate) | Scene-generation adapters                               | Empty room scenes for compositing. Swappable.                                                     |

## 10. Build Order

Re-sequenced from v1 to front-load the curation capability (the heart of
the "only edits and approvals" goal) while keeping the Etsy data pull
first, since it establishes the data model and the performance baseline
every later stage depends on. Evening/weekend pace assumed alongside the
Workiva ramp.

| **Milestone** | **Scope**                                                                                                                | **Estimate** | **Outcome**                                          |
|---------------|--------------------------------------------------------------------------------------------------------------------------|--------------|------------------------------------------------------|
| M0            | Public repo scaffold: license, .env.example, CI with secret scanning, README skeleton. Workiva addendum confirmed first. | 1 evening    | Safe to build in the open.                           |
| M1            | Etsy data pull + analytics dashboard (data model foundation).                                                            | 1 weekend    | Shop performance visible; baseline established.      |
| M2            | Discovery + viability scoring + Gate 1 curation UI.                                                                      | 2 weekends   | Curation on autopilot; operator approves in seconds. |
| M3            | EPD Edit Bridge queue processor + staging watcher (Gate 2 loop).                                                         | 1 weekend    | Approved heroes flow to LrC and back automatically.  |
| M4            | Mockup engine with AI scenes + compositing.                                                                              | 2–3 weekends | Largest manual-effort reduction lands.               |
| M5            | Listing drafts: Gelato + Printful adapters, Etsy enrichment, Gate 3 UI.                                                  | 2–3 weekends | Listing time cut to minutes.                         |
| M6            | Instagram asset generation + scheduled posting.                                                                          | 1 weekend    | Cadence on autopilot.                                |
| M7            | Feedback loop v1: tuning profiles + weekly action queue.                                                                 | 2 weekends   | System becomes self-improving.                       |

## 11. Success Metrics

- Operator time under 10 minutes per hero across the three gates
  (finishing pass 3–5 min of that); at least 75% reduction versus the
  manual baseline.

- Ingestion-to-live-listing under 24 hours including finishing.

- Instagram: 4+ posts/week sustained over rolling 30-day windows.

- Business: monthly Etsy revenue lift vs. 6-month pre-ShopSteward
  baseline; conversion-rate lift; AOV lift from bundles; IG
  engagement-rate trend.

- System health: mockup cost under \$0.50/Variant amortized;
  vision-scoring cost tracked per ingestion cycle; per-integration API
  error rates weekly; tuning rollback rate; and Gate 1 precision — the
  share of auto-surfaced candidates the operator approves, the single
  best measure of whether the viability scorer deserves more autonomy.

## 12. Risks and Mitigations

| **Risk**                                                       | **Likelihood** | **Mitigation**                                                                                                                  |
|----------------------------------------------------------------|----------------|---------------------------------------------------------------------------------------------------------------------------------|
| Etsy API rate limits or policy changes                         | Medium         | Conservative call patterns, aggressive caching, monitor developer announcements.                                                |
| Etsy enrichment pass conflicts with POD-managed listing fields | Medium         | Enrich only title/tags/description/images/price; never touch SKUs or variation structure; integration tests against a dev shop. |
| Gelato template API behavior differs from docs at scale        | Medium         | M5 spike: one template, one product, end-to-end order test before building the full matrix.                                     |
| IG Graph API business verification overhead                    | Medium         | Start verification at M0; Buffer fallback.                                                                                      |
| Vision-scoring cost or taste drift                             | Low-Med        | Score only deduplicated new candidates; cache scores; Gate 1 precision metric flags drift; prompt library versioned in repo.    |
| AI scene cost overruns                                         | Low-Med        | Scene caching, monthly budget cap, adapter swap to cheaper model.                                                               |
| Public repo leaks personal/shop data                           | Medium         | Gitignored data dir, .env pattern, gitleaks in CI, no fixtures from real shop data.                                             |
| Build time competes with Workiva ramp                          | High           | M0+M1 first for immediate value; every milestone independently useful; no deadline pressure.                                    |
| Tuning loop regressions                                        | Medium         | Versioned profiles, one-click rollback, statistical significance required.                                                      |

## 13. Open Questions

- Vision scoring rubric calibration: score a labeled set of ~100 past
  photos (sold well / listed-no-sales / never listed) to tune the prompt
  before trusting Gate 1 thresholds.

- Scene generator head-to-head (Imagen vs. Flux) on cost and realism —
  unchanged from v1, now scheduled inside M4.

- Gelato product-family template set: which families (framed poster
  sizes, paper types) earn templates in v1?

- Whether Printful's Etsy sync API supports draft-state pushes
  equivalent to Gelato's — verify during M5 spike; if not, fall back to
  Etsy-first creation with Printful linking for that provider only.

- Auto-approve threshold policy for Gate 1: earn autonomy after N
  consecutive weeks above X% precision, or keep fully manual for v1?

- Digital download delivery format standard (resolutions, aspect-ratio
  crops per pack) — inherit current PhotosByEricD conventions or
  redesign?

## 14. Appendix: Deferred to v2+

Pinterest, Facebook Shops, TikTok/Threads; customer message automation;
multi-tenant deployment and auth; Stripe billing; ML-based tuning;
conversational MCP posting from mobile; SEO automation beyond title/tag
rotation. Printify re-entry is possible later behind the same provider
adapter interface if a product family justifies it.
