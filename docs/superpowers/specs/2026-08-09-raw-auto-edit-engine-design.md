# Design: RAW auto-edit engine (event-photo path)

**Date:** 2026-08-09
**Status:** Approved (design), pending operator sign-off on dependencies + PRD amendment
**Scope of this effort:** Event-photo editing path only. No Etsy coupling.

## Background & pivot

ShopSteward is being restructured. The app's core becomes an **editing aid**
that reads RAW files and computes real, per-image develop corrections, plus a
described "look and feel" applied across a shoot. Two consequences:

1. **Automated Etsy gating is abandoned.** Scoring, viability, and RAW curation
   (the three-gate hero flow) are removed — the operator culls and picks winners
   manually in an existing external workflow.
2. **Shop-building is deferred.** Finished winner JPEGs will later be dropped
   into a defined folder from which the Etsy shop is built. That rewire is a
   *separate follow-on effort*; the listing/mockup/POD/copy/IG adapters stay
   parked (not deleted) for it.

This effort builds **only** the per-image RAW analysis → XMP sidecar engine on
the event-photo path (`shopsteward edit <path>`), with no Etsy coupling.

This supersedes the three-gate hero model for event work. **PRD v2.1 §4 and §10
need amending** (operator review — see Operator-review items).

## Architecture

All new capability lives in `src/shopsteward/editing/` — the standalone editing
module. It must not import from `adapters/etsy|printful|gelato|meta` or
`pipeline/` (import-linter already enforces this). It reuses the existing
event-sourced SQLite core, the config-over-code pattern, and the adapter
pattern.

### The two-layer model

Every edit is an **objective correction** (per image) composed with a **look**
(per shoot):

| Layer | Scope | Source | Writes to XMP |
|---|---|---|---|
| Correction | per image | RAW sensor data | White balance (Temp/Tint), Exposure, shadow-lift (luminance-range masked group) |
| Look | per job | named/described profile | Temp/Tint nudge, Contrast, Tone Curve, HSL, Split-Tone, Vibrance/Saturation |

Correction neutralizes and exposes each frame to a consistent baseline; the look
layers a creative bias on top, identically across the shoot. `xmp.compose()`
merges them deterministically: **correction first (sets the neutral baseline),
look applied as offsets on top.** WB example: correction neutralizes to a
neutral Temp/Tint; a look's warmth nudge is then added to that neutralized value.

## Components

All under `src/shopsteward/editing/` unless noted.

1. **`rawdecode.py`** — `decode(raw_path) -> DecodedImage` returning a
   reduced-resolution numpy array + camera white-balance coefficients + EXIF.
   rawpy (libraw) implementation behind a `RawDecoder` protocol so tests inject
   fakes. **No RAW files are committed** (hard guardrail: no photo files in the
   public repo).

2. **`analyze.py`** — pure functions on the decoded array producing
   `CorrectionSettings`:
   - **White balance:** gray-world / white-patch estimate → Temp/Tint. Camera WB
     coefficients from decode inform the estimate.
   - **Exposure:** histogram analysis against a target median luminance →
     exposure compensation in stops.
   - **Shadow lift:** dark-region detection → shadow-lift magnitude + the
     luminance-range bounds for the masked correction group.
   - All thresholds/targets are **config calibration knobs** (see Configuration).

3. **`adapters/look/`** — new adapter (interface + LLM impl + fake).
   `generate_look(description: str) -> LookProfile` returning structured develop
   offsets (Temp/Tint deltas, Contrast, Tone Curve points, HSL, Split-Tone,
   Vibrance/Saturation). LLM impl reuses the existing copy-adapter LLM
   provider/config. Core never imports the SDK directly. Fake returns canned
   profiles from fixtures for tests.

4. **`looks.py`** — event-sourced look store, mirroring `presets.py`. Seeds named
   looks from `config/defaults/looks/*.json` (events `look.seeded` /
   `look.updated`), read back last-write-wins by name. Describing a *new* look
   triggers the look adapter to generate a profile, which is then persisted as a
   look event — reusable on later runs and hand-tunable via config/DB.

5. **`xmp.py`** — `compose(correction, look) -> xmp_string`, writes
   `<basename>.xmp` next to the RAW. Template-based Adobe Camera Raw (crs:) XML.
   Includes the luminance-range-masked `CorrectionGroup` (RangeType="Luminance")
   for the shadow lift. Out-of-range values clamped to valid XMP ranges.

6. **CLI** — extend `shopsteward edit <path> --look <name|"description">`
   (plus `--overwrite`). Wires ingest → look resolution → per-image
   decode/analyze/compose/write → outcome events (reuse `dispatch.py` /
   `outcomes.py` patterns).

## Data flow

```
edit <folder> --look "cinematic Mexico"
 → ingest pairs RAW+JPEG (existing ingest.py)
 → resolve look:
     known name?  load from look store
     else         look adapter (LLM) generate → save to store
     [resolution runs FIRST and fails FAST — before any file is written]
 → per RAW:
     rawdecode.decode  → DecodedImage
     analyze.analyze_raw → CorrectionSettings
     xmp.compose(correction, look) → write <base>.xmp beside the RAW
 → append edit-outcome events
```

## Error handling

- **Corrupt/unreadable RAW** → skip that frame, record an outcome error, continue
  the batch. Catch rawpy errors specifically; no bare `except`.
- **Existing `.xmp` beside a RAW** → skip + warn (it may hold the operator's
  hand-edits). `--overwrite` forces a rewrite.
- **Look resolution (LLM) fails** → abort the whole job *before touching any
  files*, so there is never a half-written batch with no look applied.
- **Out-of-range settings** → clamp to valid XMP ranges in `xmp.compose`.

## Configuration (config-over-code)

Calibration knobs live in `config/defaults/` and are seeded to the DB — never
hardcoded in Python:

- WB neutral target / method selection
- Exposure target median luminance
- Shadow-lift trigger threshold, strength, and luminance-range bounds

Looks live in the DB, seeded from `config/defaults/looks/*.json`.

## Testing (fixtures only — no live APIs, no RAW files)

- **`analyze`**: synthetic numpy arrays with a known color cast / exposure →
  assert Temp/Tint/Exposure direction and magnitude within tolerance. (This is
  the required runnable self-check for the analysis heuristics.)
- **`xmp`**: compose known settings → assert expected crs: attributes and a
  valid luminance range-mask group; round-trip parse.
- **`looks`**: seed / retrieve / last-write-wins by name.
- **`look` adapter**: fake implementation only; no live LLM in tests (guardrail).
- **rawpy**: not exercised live in unit tests — decode is faked. Any real-RAW
  integration check is gated behind an uncommitted local sample.

## Rip-out (separate PR, parallel to the build)

- **Delete:** `pipeline/scorers/`, the viability/curation gating, and the
  hero-gate flow.
- **Keep parked:** listing/mockup/POD/copy/IG adapters (for the later
  winners-folder shop-building effort).
- Import-linter already forbids editing→pipeline, so the new engine is clean
  regardless of rip-out ordering.
- This is an architecture change → operator review + first PR of the effort.

## Operator-review items (per CLAUDE.md — required before code)

1. **New dependency: `rawpy`** (bundles the libraw native library).
2. **PRD amendment** — supersedes the three-gate hero model for event work and
   removes Etsy gating; PRD v2.1 §4 (flow) and §10 (milestones) need updating.
   Flagging the PRD/CLAUDE.md discrepancy as CLAUDE.md requires.

## Out of scope (explicitly, for later efforts)

- Etsy shop-building from the winners folder (rewire of parked adapters).
- Etsy-photo editing path (this effort is event-photo only).
- Any generative edit/upscale/fill on a photograph (permanent hard guardrail:
  AI never touches the photograph).
