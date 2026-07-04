# ShopSteward — Claude Code Kickoff

Save this file in the repo root. Start your Claude Code session here and paste
the "Session prompt" block below into the first message.

The amendments that were in v1 of this file (folder-pointed ingestion, hero/
mass modes, standalone editing module, landing-folder handoff, staging
template library, AI authorization, sub-agent workflow) have been folded into
`docs/PRD_v2.1.md` and `CLAUDE.md`. This file is now just workflow rules and
the open questions Fable is walking through interactively.

---

## Session prompt (paste this to start)

> **Do not write any code, run any tool, or edit any file until you have (a)
> read the four Context Files below in full, and (b) confirmed with me that
> the open questions in §2 have been resolved and folded into the PRD.**
>
> **Context files (read all four, in order):**
> 1. `CLAUDE.md` — the load-bearing architecture rules, workflow rules, and
>    hard guardrails.
> 2. `docs/PRD_v2.1.md` — the current spec. When PRD and CLAUDE.md conflict,
>    PRD wins.
> 3. `README.md` — how the modular pipeline hangs together at a glance.
> 4. `plugins/epd-edit-bridge/README.md` — the Lightroom side.
>
> Then work through §1 → §2 → §3 below.

---

## 1. Workflow rules (how we work together in every session)

These are also captured in PRD v2.1 §8 and CLAUDE.md; restated here so a
Claude Code session that starts from this file has them front-of-mind.

### 1.1 Sub-agent implementation, main-thread orchestration
All non-trivial implementation runs in sub-agents defined under
`.claude/agents/`. Standard roster:

- **`architect`** — designs, ADRs, adapter interfaces. Read-only until a
  direction is approved.
- **`python-impl`** — Python within an approved design. Constrained to
  `src/`, `tests/`, `config/`.
- **`test-author`** — pytest against recorded fixtures. Never live APIs.
- **`reviewer`** — reviews sub-agent output against CLAUDE.md guardrails
  and the current PRD milestone *before* the main session shows me anything.
- **`lua-impl`** — Lightroom plugin only. Constrained to
  `plugins/epd-edit-bridge/`.

Propose these five as concrete `.claude/agents/*.md` files as your first
concrete artifact.

### 1.2 CE review on every major decision
Nothing below ships without my explicit approval:

- Architecture or adapter-interface changes
- Amendments to CLAUDE.md, the PRD, or `.claude/settings.json`
- New dependencies (Python, npm, or system)
- New external services or API providers
- AI model / provider selection
- Anything touching secrets, auth, or the credentials boundary
- The first PR in each milestone

**Format for a CE review request:** (1) what you're proposing, (2) two or
three alternatives you considered and why you rejected them, (3) impact on
the guardrails in CLAUDE.md, (4) the smallest test that would prove it works,
(5) what would make you roll it back. One screen.

### 1.3 C-Suite critique before finalizing anything big
Before finalizing a major design, run it through a C-Suite panel and show me
the transcript:

- **CTO** — architecture, complexity budget, operational fragility.
- **CFO** — unit economics (per-listing cost, AI spend, amortized mockup
  cost against realistic Etsy conversion).
- **CMO** — will this actually move product on Etsy? Does the IG loop close?
- **CPO** — operator experience across the three gates; friction hunt.
- **Chief Legal / IP** — Etsy ToS, POD provider ToS, Instagram Graph API
  policy, invention-assignment posture, public-repo hygiene.

Each voice: 2–4 sentences of critique + at least one concrete improvement I
haven't already thought of. Surface the strongest ideas as CE review
requests.

### 1.4 Use every skill available
Reach for the skills you have when they apply — `product-management:write-spec`
for spec deltas, `engineering:architecture` for ADRs, `engineering:code-review`
before I see any PR, `engineering:testing-strategy` before writing tests,
`docx` / `pptx` when I ask for a document or deck, `data:build-dashboard` for
performance surfaces, `frontend-design` for UI. If a skill fits, use it and
cite what you used.

### 1.5 No live APIs until I say so
No calls to Etsy, Printful, Gelato, Instagram, Anthropic, Google AI, or
Replicate from any code path until the specific adapter, recorded fixtures,
and first smoke test are approved by me. Everything runs against fakes until
then.

### 1.6 Never assume — ask
Load-bearing question, unanswered → stop and ask. "I'll go with X for now"
is not acceptable unless I've explicitly said "just pick one." One line:
"Blocked on: _____ — need your call before I proceed."

---

## 2. Open questions (resolving via Fable, then folded into the PRD)

I am walking through these interactively via Fable outside Claude Code. When
Claude Code starts, confirm with me that they are all resolved and folded
into PRD v2.1 — do not begin implementation on a stage whose questions
haven't landed in the PRD yet.

**Ingestion & editing**
1. Hero vs. mass mode selection — CLI flag `--mode {hero,mass}`, UI toggle,
   or per-folder marker file?
2. Mass mode: how is the batch preset family chosen — invocation flag, folder
   name convention, or EXIF inference?
3. Mass mode output: folder of exported JPEGs, Lightroom collection handoff,
   or both? Where does the client gallery / delivery URL live?
4. Standalone editing module packaging — separate PyPI-installable package
   (`shopsteward-edit`), separate console script in the same package, or
   subpackage with a distinct CLI?

**Landing folder & Etsy handoff**
5. Landing folder — manual drag-target, editing-module output flag
   (`--send-to-etsy`), or both?
6. Does the Etsy pipeline re-score anything from the landing folder, or trust
   that anything present is already commercially approved?

**Staging templates**
7. Source — hand-curated real photos from real rooms, AI-generated empty
   rooms, or both (with priority order)?
8. Blank-region marking — alpha mask PNG paired with each template,
   coordinates in a sidecar JSON, or CV-detected at composite time?
9. Target library size at v1 launch (5, 15, 50) and who builds it?

**AI provider selection**
10. Vision scoring: Claude Opus for all, or Haiku 4.5 for triage + Opus for
    borderline? Budget ceiling?
11. Staging-template expansion (empty rooms): Imagen, Flux via Replicate, or
    hold on any AI generation until the hand-curated library is exhausted?
12. Copy generation (titles/tags/descriptions/IG captions): one model, or
    specialize? Want a house voice/style guide as a first artifact?

**Workflow**
13. Sub-agent model policy — top-tier for `architect`/`reviewer` and
    faster/cheaper for `python-impl`/`test-author`/`lua-impl`, or top-tier
    throughout?
14. C-Suite critique cadence — every milestone kickoff, every major decision,
    or only when I ask?
15. Working AI budget cap per month during the build phase before revenue
    data justifies more?

**Repo & IP**
16. ShopSteward enumerated as pre-existing work on the Workiva
    invention-assignment addendum — confirmed? If not, repo stays private
    until it is.

---

## 3. First actions (after §2 is fully resolved and folded into the PRD)

1. Read back your understanding of the current PRD (v2.1) as a C-Suite
   critique. What's fragile? What did I miss?
2. Propose the sub-agent roster (§1.1) as concrete `.claude/agents/*.md`
   files for CE review.
3. Propose the M0 completion checklist — what's left before M1 (Etsy data
   pull) can start? What configuration, credential setup, or discussion
   still needs to happen?
4. Confirm the CI stack works end-to-end on a trivial PR (gitleaks + ruff +
   pytest all green on a doc-only change).

Only after all four are approved do we start writing feature code. M1 is
first — not editing (M2 territory), regardless of how tempting the
standalone editing module is to build first.

---

## 4. Standing reminders

- Public repo. Nothing personal, nothing shop-specific, no photos in git.
- `data/` and `.env*` are radioactive. `.claude/settings.json` enforces this;
  don't try to work around it.
- One milestone per PR. PRD §10 has the current milestone table.
- If this file drifts from CLAUDE.md or the PRD, stop and flag it. The PRD
  is the living spec; this kickoff is a snapshot.
