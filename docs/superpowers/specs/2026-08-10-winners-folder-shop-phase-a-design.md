# Design: Winners-folder shop-building — Phase A (digital + vision-assisted copy)

**Date:** 2026-08-10
**Status:** Approved (design), pending operator sign-off on the vision model/pricing pin + any live smoke.
**Effort #2, Phase A of 2.** Phase B (Gelato physical POD) is a later, separate spec.

## Background

The pivot deferred shop-building; the operator now drops finished winner JPEGs
into a folder and the app builds the Etsy shop from them. **Most of this
pipeline already exists** and is landing-file-keyed:

- `pipeline/landing.py::scan_landing` watches a configurable folder
  (`data/landing/`), validates format/resolution, and records each file as
  `landing.file_observed` (with width/height/format), counting `manual_drops`
  for files with no upstream pipeline match.
- `pipeline/listings/drafts.py::build_drafts` walks `proj_landing_files WHERE
  status='valid'`, already tolerates `photo_id IS NULL` (mints a synthetic
  `file-<file_id[:12]>` id), builds copy/images/pricing, and drafts to Etsy.
- `pipeline/listings/copy.py::_build_inputs` already takes a `landing_file_id`,
  reads dimensions from `proj_landing_files`, and only reads `proj_scores` when
  a `photo_id` is present.
- The gating ripout **kept** `tuning_profile.vision` (model/pricing/cap),
  `live_gate.live_vision_open`, `proj_scores`, and all of `adapters/vision/`.
  Only `pipeline/vision_factory.py` (a thin builder) was deleted.

So the gap for Phase A is narrow:

1. **Vision-for-copy** — the deleted scoring pipeline was the only thing writing
   the subject/room-style/risk signals `copy.py` wants. Manual winners have
   none, so copy is generic. Phase A adds a gated vision pass over each winner
   JPEG that repopulates those signals.
2. **Thread the signal to copy** for photo-less winners.
3. **One-command orchestration** so "drop winners → build shop" is smooth.

Digital-direct listings only in Phase A; Gelato physical is Phase B.

## Architecture constraints

All work is in `pipeline/` (allowed to import `adapters/vision` and to use
`pipeline.live_gate` / `pipeline.tuning`). No editing-module involvement. No live
external APIs by default — vision/Etsy calls are gated (flag + key) and exercised
against fixtures in tests (CLAUDE.md).

## Component 1 — Vision-for-copy

**`pipeline/listings/vision_copy.py`** (new): for each valid landing winner that
lacks a real `photo_id`, run a vision adapter on the winner JPEG and emit a
`photo.scored` event keyed by the synthetic id `file-<file_id[:12]>` (the same
convention `build_drafts` already uses), populating `proj_scores`
(subject, strongest_room_style, one_risk, rationale). This is a **copy helper,
not a gate** — it never rejects a photo and never edits it (vision only reads a
downscaled copy; AI-never-touches-the-photo holds).

- **Adapter build:** reconstruct the minimal builder the deleted
  `vision_factory` provided — a `build_vision_adapter(profile, *, live)` local to
  `listings/` that returns the fake fixture adapter offline, or the OpenRouter/
  Gemini live adapter when `live` and `live_vision_open(provider)`.
- **Config:** reuse `tuning_profile.vision` (provider, model ids, pricing,
  `monthly_soft_cap_usd`). No new config block.
- **Gating:** reuse `live_gate.live_vision_open(provider)` +
  `live_vision_error`. `--live-vision` flag. Offline default = fixture verdict.
- **Cost cap:** reuse the existing vision cost handling if present; otherwise
  ledger an `llm.call` event (`feature="vision_copy"`) and refuse past
  `vision.monthly_soft_cap_usd`, mirroring the look-LLM cap. (Confirm during the
  plan whether the old scoring path's ledger helper survived the ripout; reuse it
  if so, else add a small local one.)
- **Idempotent:** skip a winner that already has a `photo.scored` row for its
  synthetic id unless `--regenerate`.

## Component 2 — Thread vision signals into copy

`build_drafts` already computes the synthetic `file-<id>` for photo-less rows.
Ensure the copy path receives that synthetic id as `photo_id` so
`copy._build_inputs` finds the `proj_scores` row written by Component 1. Minimal
change: where `build_drafts` calls the copy builder for a row with
`photo_id IS NULL`, pass `file-<file_id[:12]>` as the effective photo_id. Copy
behavior is unchanged when no score row exists (still degrades gracefully to
generic copy) — so a winner built without `--live-vision` still produces a valid
(blander) listing.

## Component 3 — Winners → drafts orchestration

**`shopsteward shop build <folder>`** (new sub-app, or extend the listings CLI):
runs the full manual-winner path in order —
`scan_landing(folder)` → `vision_copy` (gated) → mockups (existing M4 staging-
template compositing on the winners) → `build_drafts`. Flags: `--live-vision`,
`--live-copy`, `--live-etsy-write`, `--regenerate`, all default **off**
(fixtures). Prints a summary (observed / scored / mockup sets / drafts built).
Gate 3 (`listings/gate3` publish approval) remains the one human touchpoint and
is unchanged.

Note: digital listings use staged-room **mockup images** (M4), so mockups must
run on winners as part of the orchestration — the machinery already exists;
Phase A just sequences it.

## Data flow

```
shop build <winners_folder> [--live-vision ...]
 → scan_landing(folder)         # records landing.file_observed for each winner
 → vision_copy (gated)          # per photo-less winner: vision -> photo.scored (file-<id>)
 → mockups                      # existing staging-template compositing on winners
 → build_drafts                 # copy (uses vision signals) + images + price -> proj_listing_drafts
 → [operator] Gate 3 publish    # existing approval + push (gated live Etsy)
```

## Error handling

- A winner whose vision call fails → skip scoring that frame, continue; copy
  degrades to generic (no hard failure).
- Vision cost cap hit → refuse the vision step with a clear message before
  spending; the rest can still build (generic copy) if the operator proceeds
  without `--live-vision`.
- Unreadable/invalid winner → already handled by `scan_landing` validation
  (`landing.file_invalid`).
- All live steps refuse up front when their flag is set but the gate is closed.

## Testing (fixtures only — no live APIs)

- `vision_copy`: fake vision adapter → `photo.scored` written under the synthetic
  id; idempotent skip; `--regenerate` re-runs; cost-cap refusal via seeded ledger.
- copy threading: a photo-less winner with a fixture vision row produces copy
  that includes the subject/style signals; without a row, still valid generic
  copy.
- orchestration: stub winners folder + fakes end-to-end → drafts appear in
  `proj_listing_drafts`; summary counts correct.
- Live vision/Etsy paths: `respx`-mocked only.

## Operator-review items (per CLAUDE.md)

1. **Vision model + pricing** — confirm/pin `tuning_profile.vision` model ids +
   pricing for the copy-vision use (reused from the old scoring config; verify
   still current).
2. **Live smokes** — a specific `--live-vision` and `--live-etsy-write` smoke,
   operator-approved, before any real API calls.

## Out of scope (Phase B / later)

- Gelato physical POD (catalog, print-file compositing, R2 hosting, variation
  pricing) — Phase B.
- Instagram promotion + the performance feedback loop — later.
- Any re-introduction of automated RAW gating/curation (permanently removed).
