# Auto White Balance + Tint (R2/R3) Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Colorimetric auto WB/tint written as absolute `crs:Temperature`/`crs:Tint`, **default OFF**, calibratable, no regression when off.

**Spec:** `docs/superpowers/specs/2026-08-11-auto-white-balance-design.md`

## Task 1: `whitebalance.py` estimator (the colorimetry — reviewed)
**Files:** create `src/shopsteward/editing/whitebalance.py`, `tests/editing/test_whitebalance.py`
- [ ] Implement `estimate_wb(decoded, knobs) -> (temperature_k:int, tint:int)`: as-shot neutral (from `decoded.wb_multipliers`) → camera-RGB → XYZ (via `decoded.xyz_matrix`; **verify matrix direction with an in-code sanity assert**, don't assume) → xy → **McCamy CCT** (cite formula) + **Planckian-locus tint** (Duv → ACR tint units; sign verified at calibration). Optional `grayworld_blend` (default 0) on `decoded.rgb`. Apply `wb_temp_offset_k`/`wb_tint_offset`. Clamp Temp 2000–50000, Tint −150..+150.
- [ ] Tests: D65-ish neutral → ~6000–6600 K & tint≈0; tungsten-ish → lower K; green-biased → tint toward green; matrix-direction sanity; offsets applied. Use a known `xyz_matrix` + multipliers in `DecodedImage`.
- [ ] `uv run pytest tests/editing/test_whitebalance.py -v` → pass. `ruff`. Commit `feat(editing): colorimetric WB/tint estimator`.

## Task 2: wire (default OFF)
**Files:** `rawdecode.py`, `models.py`, `analyze.py`, `xmp.py`, `config/defaults/editing.json`, tests
- [ ] `rawdecode.DecodedImage`: add `xyz_matrix: np.ndarray | None = None`; `RawpyDecoder` sets it from `raw.rgb_xyz_matrix[:3]`; `FakeRawDecoder` passes through.
- [ ] `models.CorrectionSettings`: add `temperature: int | None = None`, `tint: int | None = None`; REMOVE the now-dead `temp_nudge`/`tint_nudge` (grep first; drop from `_cast_nudge`/`average_corrections`/analyze + their tests). (`_cast_nudge` can be deleted entirely — it was the recorded-only path.)
- [ ] `analyze.analyze_raw`: if `knobs.get("auto_white_balance")`, set `temperature`/`tint` from `estimate_wb`; else leave `None`. `average_corrections`: average temp/tint when present.
- [ ] `xmp.compose`: if `correction.temperature`/`tint` not None → emit `crs:WhiteBalance="Custom"` + `crs:Temperature` + `crs:Tint`; else `crs:WhiteBalance="As Shot"` (unchanged).
- [ ] `editing.json`: add `auto_white_balance:false`, `grayworld_blend:0.0`, `wb_temp_offset_k:0`, `wb_tint_offset:0` to `correction`.
- [ ] Tests: analyze on→temp/tint populated, off→None; xmp Custom vs As-Shot (regression guard: default off still emits As Shot, no Temp/Tint); update `test_config_knobs`; fix any test referencing removed nudges.
- [ ] `uv run pytest tests/editing tests/adapters/look -q` green. `ruff`, `lint-imports`. Commit `feat(editing): wire auto WB/tint (default off)`.

## Task 3: full gate + PR
- [ ] `uv run pytest -q` green; `ruff`; `lint-imports`; `uv run shopsteward edit --help`.
- [ ] Confirm default-off = as-shot (a test asserts no crs:Temperature when auto_white_balance false).
- [ ] Push + PR. Body: default-OFF, requires operator calibration (`wb_temp_offset_k`/`wb_tint_offset`) + Lightroom verify before enabling; approximate (not exact-ACR); closes R2/R3.

## Self-review
- Estimator standard colorimetry w/ verified matrix direction (T1); default-off no-regression guard (T2/T3); dead nudge code removed; calibration knobs; approximate + operator-verify called out. No exact-ACR claim.
