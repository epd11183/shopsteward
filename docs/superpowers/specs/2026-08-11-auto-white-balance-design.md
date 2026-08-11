# Design: Colorimetric auto white balance + tint (R2/R3)

**Date:** 2026-08-11
**Status:** Approved (design). Ships **default-OFF** behind a config knob; requires
an operator calibration + Lightroom verify before enabling. Preserves today's
as-shot behavior until then (no regression to the 468 already-edited photos).
**Closes audit items R2 (white balance/temperature) and R3 (tint).**

## Problem (verified, not assumed)

The requirement names automatic white-balance/temperature and tint correction.
Today `xmp.py` hardcodes `crs:WhiteBalance="As Shot"` and never writes
`crs:Temperature`/`crs:Tint`; the computed `tint_nudge` is dropped. We cannot
write ACR's *exact* Temperature/Tint because Lightroom derives them from Adobe's
proprietary per-camera profile — rawpy gives WB **multipliers**
(`camera_whitebalance`) + the camera→XYZ matrix (`rgb_xyz_matrix`, 4×3 verified
present), not ACR units. So we compute an **approximate** illuminant CCT + tint
by a standard colorimetric method (as used by darktable/RawTherapee) and write it
as absolute `crs:Temperature`/`crs:Tint`. Approximate by nature; made trustworthy
by a per-camera calibration offset + a Lightroom verify.

## Method (`editing/whitebalance.py`)

`estimate_wb(decoded, knobs) -> (temperature_k: int, tint: int)`:

1. **Illuminant in camera RGB.** The as-shot neutral (the illuminant the camera
   balanced to) is derived from `camera_whitebalance` — a neutral surface reads
   proportional to the per-channel multipliers. (Implementer: confirm the
   convention by checking a neutral maps to a plausible near-white XYZ; do NOT
   assume the matrix/multiplier direction — verify with a sanity assertion.)
2. **Camera RGB → XYZ** via `rgb_xyz_matrix` (first 3 rows; verify direction).
3. **XYZ → xy chromaticity → CCT** via **McCamy's cubic approximation** (cite the
   formula in code). **Tint** = signed distance from the Planckian locus (Duv),
   scaled to ACR tint units (green negative / magenta positive — verify sign
   against ACR at calibration).
4. **Optional gray-world residual** (`grayworld_blend` knob, default 0): blend the
   as-shot illuminant estimate with a gray-world estimate on the decoded image,
   for scenes where the camera's own WB was wrong. Default 0 = trust the camera's
   illuminant estimate, just expressed as Temp/Tint.
5. **Calibration offsets** (`temp_offset_k`, `tint_offset`, default 0): a
   per-camera constant added to align our estimate to what Lightroom shows for
   the operator's body (the "leave the calibration knob" principle — a real
   sensor/profile gap a model can't see).
6. Clamp to valid ACR ranges (Temperature ~2000–50000 K; Tint −150..+150).

Unit-tested against known inputs: a ~D65 neutral → ~6000–6600 K, tint ≈ 0; a warm
(tungsten-ish) illuminant → lower K; a green-biased illuminant → tint toward
green. Plus a matrix-direction sanity test.

## Wiring

- **`rawdecode.py`**: add `xyz_matrix` (the 3×3 from `rgb_xyz_matrix`) to
  `DecodedImage` (alongside `wb_multipliers`); `RawpyDecoder` populates it;
  `FakeRawDecoder` accepts it (tests supply a known matrix).
- **`models.py` `CorrectionSettings`**: add `temperature: int | None`,
  `tint: int | None` (absolute ACR values; `None` = leave as-shot). Keep the old
  `temp_nudge`/`tint_nudge`? Remove them — replaced by real values (they were
  never written; deleting closes the dead-code path the audit flagged).
- **`analyze.analyze_raw`**: when `auto_white_balance` is true, call `estimate_wb`
  and set `temperature`/`tint`; else leave both `None` (as-shot). `average_corrections`
  averages them when present (batch-lock consistency).
- **`xmp.compose`**: if `correction.temperature`/`tint` are set, emit
  `crs:WhiteBalance="Custom"` + `crs:Temperature` + `crs:Tint`; else the current
  `crs:WhiteBalance="As Shot"` (unchanged default). The look still owns no WB.

## Config (`config/defaults/editing.json` `correction` block)
```
"auto_white_balance": false,      # default OFF — as-shot until calibrated+verified
"grayworld_blend": 0.0,           # 0 = trust camera illuminant; up to 1.0
"wb_temp_offset_k": 0,            # per-camera calibration to match Lightroom
"wb_tint_offset": 0
```

## Safety / no-regression
- **Default OFF**: with `auto_white_balance:false`, `analyze` leaves temp/tint
  `None` and `xmp` emits `As Shot` exactly as today — the shipped 468 and every
  existing test are unchanged.
- Turning it on is a deliberate operator step after calibration.

## Testing (fixtures only)
- `whitebalance`: McCamy CCT + tint on known XYZ/matrix inputs (D65, tungsten,
  green cast); matrix-direction sanity.
- `analyze`: `auto_white_balance` on → temperature/tint populated in the expected
  direction; off → both `None` (as-shot).
- `xmp`: temp/tint set → `WhiteBalance="Custom"` + `crs:Temperature`/`crs:Tint`
  emitted and clamped; unset → `As Shot`, no Temp/Tint (regression guard).
- Full suite stays green with the feature off (default).

## Operator calibration + verify (required before enabling)
1. On one representative RAW from the operator's camera, run with
   `auto_white_balance:true`, open the `.xmp` in Lightroom, compare our
   Temperature/Tint to LR's As-Shot readout; set `wb_temp_offset_k`/`wb_tint_offset`
   to close the gap; confirm mixed-lighting frames neutralize.
2. Only then enable `auto_white_balance` for real runs.

## Out of scope
- Exact ACR-matching WB (needs Adobe's DCP profile — not achievable from a sidecar).
- Per-region/subject WB. Autonomy pillar (separate program).
