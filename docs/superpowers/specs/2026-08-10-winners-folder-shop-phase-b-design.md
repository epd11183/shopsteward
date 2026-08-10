# Design: Winners-folder shop-building — Phase B (Gelato physical POD)

**Date:** 2026-08-10
**Status:** Approved (design), pending operator sign-off on the live R2 / Gelato / Etsy smokes.
**Effort #2, Phase B of 2.** Phase A (digital + vision-copy) shipped in PR #24.

## Background

Physical print-on-demand is **already built** (M5b): `pipeline/listings/pod/`
provides `build_pod_drafts` / `dry_run_pod_build`, aspect routing + variant
selection + DPI checks (`catalog.py`), retail/economics/floor pricing
(`pricing.py`), print-file source resolution + R2 hosting (`printfile.py`), the
Gelato adapter (`adapters/pod`) and R2 print-file adapter
(`adapters/printfile`), a configured `config/defaults/pod.json` (aspects, Gelato
routing, price ceilings, print-file rules), and a standalone `pod build` CLI.

Crucially it already fits the winners flow:
- `build_pod_drafts._eligible_landing_rows` walks `proj_landing_files WHERE
  status='valid'`, tolerates `photo_id IS NULL`, and uses the same synthetic
  `file-<file_id[:12]>` convention as Phase A.
- `printfile.resolve_print_source_path` explicitly falls back to the winner's
  own JPEG when no TIFF master sibling exists ("an operator who lands only a
  JPEG still gets a print file — just not the preferred one").
- Low-resolution winners simply yield fewer/smaller variants — `catalog.py`
  drops variants that fail `min_dpi`; no hard failure.
- POD-first listing creation (Gelato creates the product + pushes the Etsy
  draft, we then enrich) is the built design (CLAUDE.md rule).

So Phase B is **wiring, not building**: add POD to the `shop build`
orchestration, confirm the winners→physical path with fixtures, and leave the
provider calls behind their existing live gates for an operator smoke.

## Decisions
- `shop build <folder>` produces **both** digital and physical drafts by default,
  against fixtures. Live provider calls stay behind explicit flags.
- **Separate listings per winner** (existing design): one digital-direct listing
  + one physical POD listing (with size/material variants). Unchanged.

## Scope

### 1. Wire POD into `run_shop_build`
`src/shopsteward/shop.py::run_shop_build` currently runs
scan → vision-copy → mockups → `build_drafts` (digital). Add a step that also
runs `build_pod_drafts(...)` (physical) after the digital build. Add the
`--live-printfile` gate (`live_printfile_open` / `live_printfile_error`) to the
up-front refusal set alongside vision/copy/etsy. The CLI `shop build` gains a
`--live-printfile` flag (default off). Return summary adds physical counts
(pod drafts built / variants / dropped).

### 2. Verify POD copy gets the vision signals
POD listings need copy too. Confirm how `build_pod_drafts` produces copy: if it
calls `copy.generate_copy` with the landing row's `photo_id` (which is `None` for
manual winners), it needs the **same synthetic-id threading** Phase A applied to
the digital `build_drafts` (`effective_photo_id = row["photo_id"] or
f"file-{file_id[:12]}"`), so the `proj_scores` vision signals reach POD copy. If
`build_pod_drafts` reuses the digital draft's copy (shared), no change is needed.
The plan resolves this by reading `pod/build.py` first; apply the same threading
fix only if POD builds its own copy from a null photo_id.

### 3. Print source + DPI
No code change expected — `resolve_print_source_path` already falls back to the
winner JPEG and `catalog.py` drops sub-DPI variants. The plan adds a test that a
manually-dropped JPEG winner produces at least one valid POD variant (and that a
deliberately tiny winner drops the large variants without erroring).

## Data flow (Phase B addition in bold)
```
shop build <winners_folder> [--live-* ...]
 → scan_landing → vision_copy → mockups
 → build_drafts        (digital listing drafts)
 → build_pod_drafts    (physical POD drafts: Gelato product + Etsy draft, then enrich)   ← NEW
 → [operator] Gate 3 publish
```

## Error handling
- All live steps (`--live-vision/-copy/-printfile/-etsy-write`) refuse up front
  when their flag is set but the gate is closed.
- A winner that yields no valid POD variant (too low-res for any size) is skipped
  with a recorded reason (existing `PodDroppedVariant` / skipped-reason logic),
  not a hard failure; the digital listing still builds.
- POD build is idempotent by its existing `draft_id` (no duplicate drafts on
  re-run).

## Testing (fixtures only — no live APIs)
- `shop build` end-to-end with fixtures now also produces rows in the POD drafts
  projection for a valid winner; summary counts include physical.
- Winner-JPEG-as-print-source: a valid winner yields ≥1 POD variant; a tiny
  winner drops large variants without error.
- POD copy carries the vision subject signal for a photo-less winner (if §2
  requires the threading fix, a test mirrors Phase A's `test_copy_manual_winner`).
- Live R2/Gelato/Etsy paths: fakes/`respx` only.

## Operator-review items (per CLAUDE.md)
1. **Live smokes** — a specific, operator-approved run of `--live-printfile`
   (R2), Gelato product creation, and `--live-etsy-write` before any real calls.
   Confirm R2 bucket creds + Gelato routing in `pod.json` are current.
2. **Pricing** — confirm `pod.json` price ceilings / floors and the Gelato unit
   costs still reflect current provider pricing (margins).

## Out of scope (later)
- Instagram promotion + the performance feedback loop.
- Additional POD providers (routing is Gelato-only today).
- Any TIFF-master ingestion workflow (winners are the finished JPEGs; a master
  is used only if the operator happens to land one alongside).
