# Design: Winners-folder shop-building — Phase B (physical POD: costing + print files)

**Date:** 2026-08-10 (rev. 2 after verifying the parked POD's actual slice state)
**Status:** Approved (design). Scope corrected: wire the built POD slices 1–2;
Gelato product-create + Etsy push + enrichment are deferred to Phase C.
**Effort #2, Phase B of 3.** Phase A (digital + vision-copy) shipped in PR #24.

## Corrected understanding of the parked POD

Reading the code (not the earlier assumption): `pipeline/listings/pod/` is built
through **slices 1–2 only**:

- **Built:** `build_pod_drafts` / `dry_run_pod_build` — per-winner variant
  selection + aspect routing + DPI checks (`catalog.py`), retail/economics/floor
  pricing (`pricing.py`), and print-file resolve + R2 hosting (`printfile.py`,
  `factory.build_print_file_host(live=...)`). It emits `listingdraft.created →
  variants_selected → priced → print_file_prepared → print_file_hosted`, then
  **stops** (its own docstring: "provider create/poll/link/enrich is slice 3/4").
- **NOT built (deferred to Phase C):** Gelato product creation (create→poll→link),
  the Etsy draft push for physical, and copy/image enrichment. Only a **fake**
  Gelato adapter exists (`adapters/pod/fake.py`); there is **no live Gelato
  adapter** and no `build_pod_adapter` factory yet.

Winners-friendliness (verified): `build_pod_drafts` walks
`proj_landing_files WHERE status='valid'`, tolerates `photo_id IS NULL`;
`resolve_print_source_path` falls back to the winner's own JPEG when no TIFF
master exists; `catalog.py` drops sub-DPI variants gracefully. POD needs no
mockup set.

## Phase B scope (this effort)

**Wire the built POD slices 1–2 into the `shop build` orchestration**, so one
command produces, per winner: a digital listing draft (Phase A) **and** a
physical POD draft that is variant-selected, priced, and has its print file
hosted. This delivers costed physical drafts with hosted print files — the
inputs Phase C will turn into real Gelato/Etsy listings. No live external calls
by default; R2 hosting is gated.

### 1. Wire `build_pod_drafts` into `run_shop_build`
`src/shopsteward/shop.py::run_shop_build` currently ends at `build_drafts`
(digital). Add, after it:
```python
host = build_print_file_host(live=live_printfile)   # fake unless --live-printfile
pod = build_pod_drafts(conn, user_id, print_file_host=host)
```
- Add `live_printfile: bool = False` param + up-front gate
  (`live_printfile_open` / `live_printfile_error`) to the refusal set.
- The CLI `shop build` gains `--live-printfile` (default off).
- Return-summary gains physical counts from `PodBuildReport` (drafts built /
  variants kept / dropped).
Mirror the exact wiring in `pod/cli.py::build` (host construction + gate).

### 2. Print source + DPI (no code change expected)
`resolve_print_source_path` already falls back to the winner JPEG;
`catalog.py` drops variants that fail `min_dpi`. The plan adds a test that a
valid winner yields ≥1 kept POD variant and a deliberately tiny winner drops the
large variants without erroring.

### 3. NOT in scope (Phase C)
- Gelato product create/poll/link (slice 3) + a **live Gelato adapter** +
  `build_pod_adapter` factory.
- Etsy draft push for physical + copy/image enrichment (slice 4). Because copy
  enrichment is slice 4, the Phase-A vision-copy signals are **not** consumed by
  POD in Phase B — that wiring belongs to Phase C.

## Data flow (Phase B addition in bold)
```
shop build <winners_folder> [--live-printfile ...]
 → scan_landing → vision_copy → mockups → build_drafts   (digital, Phase A)
 → build_pod_drafts (host=fake|R2)                        ← NEW: select+price+host print file
      emits listingdraft.* up to print_file_hosted; STOPS before provider create
 → [Phase C] Gelato create/poll/link → Etsy push → enrich
```

## Error handling
- `--live-printfile` set but R2 gate closed → refuse up front
  (`live_printfile_error`), before scan/spend.
- A winner too low-res for any variant → recorded skip
  (`listingdraft.pod_skipped`), not a hard failure; the digital draft still
  builds.
- `build_pod_drafts` is idempotent by its existing `draft_id` (no dupes on
  re-run); `--regenerate` in `shop build` continues to govern only vision-copy.

## Testing (fixtures only — no live APIs)
- `shop build` end-to-end (fixtures) now also produces POD draft rows /
  `print_file_hosted` events for a valid winner; summary includes physical counts.
- Winner-JPEG-as-print-source: valid winner → ≥1 kept variant; tiny winner →
  large variants dropped, no error.
- `--live-printfile` refusal when the R2 gate is closed.

## Operator-review items (per CLAUDE.md)
1. **Live R2 smoke** — a specific `--live-printfile` run (real bucket) once
   operator-approved; confirm R2 creds. No Gelato/Etsy calls happen in Phase B.
2. **Pricing** — confirm `pod.json` ceilings/floors + Gelato unit costs still
   reflect current margins (they drive variant kept/dropped + retail price).

## Out of scope (Phase C and later)
- Slices 3–4: Gelato product creation, live Gelato adapter, Etsy physical draft
  push, copy/image enrichment.
- Instagram promotion + performance feedback loop.
- Additional POD providers (Gelato-only routing today).
