# ShopSteward

**A modular workflow tool for a photography business: a standalone editing
module for event work, and an Etsy pipeline that runs a print-on-demand +
digital-download shop with three taps a day.**

ShopSteward has two halves that meet at a single folder:

- **The editing module** (standalone — no marketplace code loaded) ingests a
  folder of mixed RAW + JPEG files, pairs them, and applies preset families
  via the EPD Edit Bridge Lightroom plugin. Two modes: **hero** (single-image,
  feeds the Etsy pipeline) and **mass** (batch event work — weddings, races,
  brewery events — exported gallery-ready with no Etsy coupling).
- **The Etsy pipeline** watches a landing folder for photos flagged for sale,
  then handles viability scoring, staging-template mockups (the AI generates
  empty rooms — it never touches your photograph), listing drafts across
  digital downloads and physical products fulfilled by **Printful** and
  **Gelato**, Instagram promotion, and a performance feedback loop that tunes
  itself from your actual sales data.

On the hero/Etsy path you stay in the loop at exactly **three gates**:

| Gate | What you do | Time |
|---|---|---|
| **1 — Curate** | Approve/reject scored candidates in a batch UI | ~5 sec/image |
| **2 — Finish** | Quick Lightroom pass on each hero; exporting *is* the approval | 3–5 min/hero |
| **3 — Publish** | One-tap review of drafted listings + queued Instagram posts | ~1 min/hero |

Everything between the gates runs unattended. Mass mode has its own
lighter-weight flow: point at a folder, pick a preset family, get
delivery-ready exports.

> **Status: pre-alpha.** Built in the open, nights and weekends, to run
> [PhotosByEricD](https://www.etsy.com/shop/PhotosByEricD) first. The spec is
> in [`docs/PRD_v2.1.md`](docs/PRD_v2.1.md); the milestone plan is §10.

## How it works

```
 folder of RAW+JPEG ──▶ shopsteward ingest (UI or CLI, --mode hero|mass)
        │
        ├─ mass ──▶ batch preset via EPD Edit Bridge ──▶ LrC collection
        │           + delivery-ready JPEG exports          (event done)
        │
        └─ hero ──▶ Stage 1 scoring ──▶ [Gate 1 Curate]
                                             │
              EPD Edit Bridge applies preset ▼
   [Gate 2: finish ◀── "Needs Finishing" collection ◀── job queue
    + export to landing folder]
        │
        ▼  (landing folder = the only editing → Etsy interface)
  Stage 3 Mockups ──▶ Stage 4 Listings (Gelato/Printful → Etsy draft
  (template library    → enrichment via Etsy API; digital direct) ──▶ [Gate 3]
   + deterministic                                                      │
   compositing)        Stage 6 Instagram asset packs + scheduler  ◀─────┘
        ▲                                                               │
        └───────── Stage 5/tuning: nightly performance pulls ◀──────────┘
```

## Quickstart

```bash
git clone https://github.com/ericdipietro/shopsteward && cd shopsteward
uv sync
cp .env.example .env        # then fill in credentials — see docs/setup/
uv run shopsteward serve    # web UI: ingest, curate, publish
uv run shopsteward ingest <path> --mode mass --preset wedding   # CLI editing
```

For the Etsy pipeline you'll need your own (free) developer credentials for
Etsy, Printful, Gelato, and Instagram, plus a Gemini API key for vision
scoring and copy generation. The editing module alone needs none of them.
Setup guides live in `docs/setup/`.

The Lightroom side is a small Lua plugin, [EPD Edit
Bridge](plugins/epd-edit-bridge/) — install once via Lightroom's Plug-in
Manager.

## Design principles

- **Local-first.** Your catalog, your machine, your data. SQLite, no cloud
  dependency beyond the APIs you already use.
- **The AI never edits your photograph.** Vision models score; staging
  templates are AI-generated empty rooms; the print is composited
  deterministically with Pillow/OpenCV.
- **The editing module stands alone.** It imports nothing from the Etsy
  stack — enforced by an import-linter rule in CI. The landing folder is
  the only interface between editing and selling.
- **Everything is reversible.** Event-sourced storage, versioned tuning
  profiles with rollback, undoable Lightroom history steps.
- **Adapters everywhere.** Swap AI providers, add POD providers, or bring a
  different marketplace without touching the core.

## Contributing

Early days — issues and discussion welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md). If you run a photography POD shop and want
to be an early tester once M5 lands, open an issue and say hi.

## License

[MIT](LICENSE) © 2026 DiPietro Enterprises LLC
