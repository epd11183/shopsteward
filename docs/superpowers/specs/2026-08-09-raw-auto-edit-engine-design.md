# Design: RAW auto-edit engine (mass mode)

**Date:** 2026-08-09
**Status:** Approved (design, rev. 2 after ce-doc-review), pending operator
sign-off on the `rawpy` dependency + PRD amendment.
**Scope of this effort:** Mass-mode (event-work) editing path only. No Etsy
coupling.

## Background & pivot

ShopSteward is being restructured. The app's core becomes an **editing aid**
that reads RAW files and writes develop settings as XMP sidecars: a conservative
per-image **correction** plus a described **look** applied across a shoot. Three
consequences:

1. **Automated Etsy gating is abandoned product-wide.** Scoring, viability, and
   RAW curation (the three-gate hero flow) are removed for all modes — the
   operator culls and picks winners manually in Lightroom.
2. **Shop-building is deferred.** Finished winner JPEGs will later be dropped into
   a defined folder from which the Etsy shop is built. That rewire is a *separate
   follow-on effort*; the listing/mockup/POD/copy/IG adapters stay parked (not
   deleted) for it. See Open Questions — this parks the revenue path with no
   un-defer trigger yet, an accepted tradeoff to be revisited.
3. This effort builds **only** the per-image RAW analysis → XMP sidecar engine on
   the mass-mode path (`shopsteward edit <path>`), with no Etsy coupling.

This supersedes the three-gate model. **PRD v2.1 §4 and §10 need amending**
(operator review — see Operator-review items).

## Workflow position (load-bearing)

**ShopSteward runs first, then the operator imports to Lightroom.** The engine
writes `<basename>.xmp` next to each RAW *before* import, so Lightroom picks the
settings up on a fresh import.

> **Caveat (must be documented in operator setup):** the operator's Lightroom
> import must NOT be configured to apply a default develop preset / "Auto
> Settings" on import — that would stack on or override the sidecar. Import with
> "None" as the develop default.

Because XMP stores *resolved* develop values (not an "apply Auto WB" instruction),
the engine must write concrete numbers for every field it owns. Correction cannot
be delegated to Lightroom's Auto in a sidecar-first flow. This is why the
correction layer is deliberately conservative (below) rather than a full auto-WB
estimator.

## Architecture

All new code lives in `src/shopsteward/editing/` (editing-internal modules) and
`src/shopsteward/adapters/look/` (the one external-system adapter). The editing
module must not import from `adapters/etsy|printful|gelato|meta` or `pipeline/`
(import-linter already enforces this; note that `adapters/look/` importing
`adapters/copy/` would NOT trip that contract, but we avoid it anyway — see
Component 3). Reuses the event-sourced SQLite core, config-over-code, and the
adapter pattern.

### The two-layer model

Every edit is an **objective correction** (per image) composed with a **look**
(per shoot):

| Layer | Scope | Source | Owns in XMP |
|---|---|---|---|
| Correction | per image | RAW sensor data + EXIF | WB baseline (as-shot Temp/Tint + capped cast nudge), global Exposure, shadow-lift (local luminance-range masked group) |
| Look | per job | named/described profile | WB creative delta (bounded), Contrast, Tone Curve, HSL, Split-Tone, Vibrance/Saturation |

Correction gets each frame to a sane, consistent baseline; the look layers a
creative bias identically across the shoot.

### Composition — explicit field ownership

`xmp.compose(correction, look)` merges by the ownership table above. Per-field
rules (this is the authoritative merge contract):

- **WB Temp/Tint** — correction sets the absolute value (camera as-shot, plus a
  capped cast nudge). The look may add a *bounded* creative delta on top. Both
  the nudge and the look delta are clamped so their sum stays in valid range.
- **Exposure (global)** — correction only. The look never writes global Exposure;
  it shapes brightness via Contrast / Tone Curve instead. This prevents a
  double-exposure conflict.
- **Shadows** — correction owns shadow recovery via a *local* luminance-range
  masked CorrectionGroup. The look owns global Contrast / Tone Curve. Because the
  lift is a local masked adjustment layered after global tone in ACR's pipeline,
  a look's contrast curve does not crush it.
- **Contrast, Tone Curve, HSL, Split-Tone, Vibrance/Saturation** — look only.

### White balance — why no Kelvin estimator

Converting sensor RGB multipliers to Adobe `crs:Temperature` (Kelvin) / `crs:Tint`
requires a proprietary per-camera color profile and has no documented closed form.
We avoid it entirely:

- **Baseline** = the camera's **as-shot** Temp/Tint, read from the RAW/EXIF via
  rawpy and written through unchanged. Modern camera auto-WB is correct the large
  majority of the time.
- **Cast nudge** = a bounded gray-world check that fires *only* when a strong cast
  is detected, applied as a small capped delta on the as-shot Temp/Tint (never an
  absolute re-derivation). The cap is a calibration knob, sized so it cannot blow
  out a dominant-color scene (green field, warm hall, snow).

### Batch consistency

A `--wb-lock` / per-shoot correction mode averages the WB and exposure
corrections across the batch (or a selected sequence) and applies the shared
value to every frame, so a constant-lighting burst stays consistent instead of
drifting frame-to-frame. Default is per-image; the lock is opt-in per job.

## Components

1. **`editing/rawdecode.py`** — `decode(raw_path) -> DecodedImage` returning a
   reduced-resolution numpy array + camera as-shot WB (Temp/Tint) + EXIF, via
   rawpy (libraw), behind a `RawDecoder` protocol so tests inject fakes. **No RAW
   files are committed** (hard guardrail). The RAW is the sole pixel/EXIF source —
   a paired JPEG is NOT required (see Ingest).

2. **`editing/analyze.py`** — pure functions on the decoded array producing
   `CorrectionSettings`:
   - **WB:** pass through as-shot Temp/Tint; apply the capped cast nudge only when
     a strong cast is detected.
   - **Exposure:** histogram analysis against a target median luminance → exposure
     compensation in stops.
   - **Shadow lift:** dark-region detection → lift magnitude + the luminance-range
     bounds for the masked group.
   - All thresholds/targets/caps are **config calibration knobs** (see
     Configuration).

3. **`src/shopsteward/adapters/look/`** — new adapter (interface + LLM impl +
   fake). `generate_look(description: str) -> LookProfile` returning structured
   develop offsets (Temp/Tint delta, Contrast, Tone Curve points, HSL, Split-Tone,
   Vibrance/Saturation) via a JSON-schema-constrained LLM call. The impl carries
   its **own `openrouter.py`**, mirroring the established per-adapter pattern in
   `adapters/copy/` and `adapters/vision/` — there is no shared LLM provider
   module today, and we do not create one in this effort. Fake impl returns canned
   profiles from fixtures for tests.

4. **`editing/looks.py`** — event-sourced look store. Reuses the seed-from-config +
   last-write-wins-by-name *pattern* from `presets.py` (extract the shared bits
   into a small helper both call). Seeds named looks from
   `config/defaults/looks/*.json` (events `look.seeded` / `look.updated`).
   `LookProfile` is a distinct, richer model than `PresetFamily` (nested tone-curve
   arrays, per-channel HSL, split-tone) — it cannot reuse `PresetFamily`'s flat
   `settings` dict.
   - **Described-look keying:** a free-text description is keyed by its normalized
     text (lowercased, whitespace-collapsed). An identical later description
     resolves to the stored profile rather than regenerating. `--regenerate`
     forces a fresh LLM call. Operators are encouraged to assign a short name to a
     look they want to reuse deliberately.

5. **`editing/xmp.py`** — `compose(correction, look) -> xmp_string`, writes
   `<basename>.xmp` next to the RAW. Template-based Adobe Camera Raw (crs:) XML.
   Includes the luminance-range-masked `CorrectionGroup` (RangeType="Luminance")
   for the shadow lift. Applies the field-ownership merge rules above. Out-of-range
   values clamped to valid XMP ranges.

6. **CLI** — **add** a `shopsteward edit <path> --look <name|"description">`
   command (today `edit_app` only exposes the presets sub-app + a status command,
   so this is net-new wiring, not an extension). Flags: `--look`, `--regenerate`,
   `--overwrite`, `--wb-lock`. Wires ingest → look resolution → per-image
   decode/analyze/compose/write → outcome events (reuse the event-append patterns
   from `dispatch.py` / `outcomes.py`; note those modules are otherwise built
   around the Lightroom bridge, which this path does not use).

## Data flow

```
edit <folder> --look "cinematic Mexico"
 → ingest RAWs from the folder (JPEG sibling optional; EXIF+pixels from rawpy)
 → resolve look:
     known name?                 load from look store
     described + already stored? load by normalized-description key
     else (or --regenerate)      look adapter (LLM) generate → save to store
     [resolution runs FIRST and fails FAST — before any file is written]
 → per RAW:
     rawdecode.decode  → DecodedImage (array + as-shot WB + EXIF)
     analyze.analyze_raw → CorrectionSettings   (--wb-lock: average across batch)
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
- **Out-of-range settings** (including the summed WB nudge + look delta) → clamp
  to valid XMP ranges in `xmp.compose`.

## Configuration (config-over-code)

Calibration knobs live in `config/defaults/` and are seeded to the DB — never
hardcoded in Python:

- Cast-detection trigger threshold and **nudge cap** (Temp/Tint)
- Exposure target median luminance
- Shadow-lift trigger threshold, strength, and luminance-range bounds
- Look creative-WB-delta bound

Looks live in the DB, seeded from `config/defaults/looks/*.json`. A small set of
hand-authored seed looks (e.g. `bright-and-true`, `national-geographic`) ships so
the tool is usable without an LLM call and gives an A/B baseline against generated
looks.

## Testing (fixtures only — no live APIs, no RAW files)

- **`analyze`**: synthetic numpy arrays with a known cast / exposure → assert the
  cast nudge fires only past threshold, stays within the cap, and Exposure moves
  in the right direction/magnitude within tolerance. (Required runnable self-check
  for the correction heuristics.)
- **`xmp`**: compose known correction + look → assert crs: attributes, a valid
  luminance range-mask group, correct field ownership (look does not write global
  Exposure; WB is as-shot + nudge + clamped look delta); round-trip parse.
- **`looks`**: seed / retrieve / last-write-wins; described-look normalized-key
  resolution and `--regenerate` behavior.
- **`look` adapter**: fake implementation only; no live LLM in tests (guardrail).
- **rawpy**: not exercised live in unit tests — decode is faked. Confirm the pinned
  rawpy wheel decodes the operator's RAW formats (CR3, etc.) on the target Windows
  environment during the dependency-approval smoke, before committing.

## Rip-out (separate PR, parallel to the build)

- **Delete:** `pipeline/scorers/`, the viability/curation gating, and the
  three-gate flow (all modes).
- **Keep parked:** listing/mockup/POD/copy/IG adapters (for the later
  winners-folder shop-building effort).
- Import-linter already forbids editing→pipeline, so the new engine is clean
  regardless of rip-out ordering.
- Architecture change → operator review + first PR of the effort.

## Operator-review items (per CLAUDE.md — required before code)

1. **New dependency: `rawpy`** (bundles the libraw native library). Approval smoke:
   confirm CR3 (and any other operator formats) decode on Windows.
2. **PRD amendment** — supersedes the three-gate model and removes Etsy gating;
   PRD v2.1 §4 (flow) and §10 (milestones) need updating. Flagging the PRD /
   CLAUDE.md discrepancy as CLAUDE.md requires.

## Open questions (resolve before / during planning)

- **Un-defer trigger for shop-building.** The pivot parks the only revenue path
  (Etsy shop). What concrete date, milestone, or signal un-defers it — and is the
  Etsy path being *deferred* or effectively *abandoned*? (Raised by ce-doc-review
  product-lens; direction chosen, trigger undecided.)
- **LLM look quality gate.** The look layer is the product differentiator but has
  no acceptance step beyond range-clamping. Plan a validation pass: on a real
  shoot, compare operator-rated LLM-generated looks against the hand-authored seed
  looks, to confirm the LLM layer earns its place. (Raised by product-lens +
  adversarial.)

## Out of scope (explicitly, for later efforts)

- Etsy shop-building from the winners folder (rewire of parked adapters).
- Hero-mode / Etsy-facing editing path (this effort is mass-mode only).
- A full per-camera auto-WB Kelvin estimator (deliberately avoided; see White
  balance).
- Any generative edit/upscale/fill on a photograph (permanent hard guardrail:
  AI never touches the photograph).
