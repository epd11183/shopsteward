# RAW Auto-Edit Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a mass-mode `shopsteward edit <path>` engine that decodes each RAW, computes a conservative objective correction, composes it with a named/described "look," and writes an Adobe Camera Raw XMP sidecar next to each RAW.

**Architecture:** Standalone editing module (`src/shopsteward/editing/`) plus one external-system adapter (`src/shopsteward/adapters/look/`). RAW pixels/metadata via `rawpy`; correction is pure NumPy; the look is an LLM-generated, event-sourced, hand-tunable profile (mirrors `presets.py`). Delivery is XMP-sidecar-first (written before Lightroom import). No Etsy/pipeline coupling; no Lightroom bridge.

**Tech Stack:** Python 3.12, Pydantic v2, Typer, `rawpy` (libraw), NumPy, `httpx` (OpenRouter), SQLite event log. Tests: pytest + `respx` (HTTP), fakes for RAW decode and LLM. No live APIs, no committed RAW files.

**Spec:** `docs/superpowers/specs/2026-08-09-raw-auto-edit-engine-design.md`

---

## Design decisions locked in (read before starting)

- **WB = As Shot.** The sidecar sets `crs:WhiteBalance="As Shot"` and omits `crs:Temperature`/`crs:Tint`, so Lightroom uses the camera's as-shot WB on import. rawpy exposes WB *multipliers*, not ACR Kelvin; converting them needs a proprietary per-camera profile, so we do not.
- **Cast nudge is computed but not written by default.** `analyze` records a recommended `temp_nudge`/`tint_nudge` in the event log for a future calibration effort, but `xmp.compose` only writes WB overrides when `apply_wb_nudge` is true in config (default `false`). `ponytail:` ceiling — enabling it correctly requires per-camera Kelvin calibration.
- **Look color is Kelvin-free.** Look warmth/grade is expressed via Contrast, Tone Curve, HSL, Split Toning, Vibrance/Saturation — all absolute, no WB baseline needed. `LookProfile` carries no temp/tint deltas.
- **Correction owns:** global Exposure + a *local* luminance-range-masked shadow lift. **Look owns:** Contrast, Tone Curve, HSL, Split Toning, Vibrance, Saturation. No field is written by both.
- **Sidecar-first workflow.** Operator runs `edit` before importing to Lightroom, and must set Lightroom import develop default to "None" (documented in Task 11).
- **Bridge untouched.** `dispatch.py`/`outcomes.py`/`adapters/lightroom` are the old hero-finish path; the new engine does not use or modify them.

## File structure

Created:
- `src/shopsteward/editing/rawdecode.py` — RAW decode seam (protocol + rawpy impl + fake)
- `src/shopsteward/editing/analyze.py` — pure correction engine
- `src/shopsteward/editing/xmp.py` — compose + write sidecar
- `src/shopsteward/editing/looks.py` — event-sourced look store + resolution
- `src/shopsteward/editing/edit.py` — orchestration (`run_edit`)
- `src/shopsteward/adapters/look/__init__.py`
- `src/shopsteward/adapters/look/interface.py` — `LookProfile`, `LookAdapter`, errors
- `src/shopsteward/adapters/look/openrouter.py` — `OpenRouterLookAdapter`
- `src/shopsteward/adapters/look/fake.py` — `FixtureLookAdapter`, `FakeLookAdapter`
- `config/defaults/looks/bright-and-true.json`, `config/defaults/looks/national-geographic.json`
- `config/defaults/prompts/look_profile.txt`
- Tests under `tests/editing/` and `tests/adapters/look/`

Modified:
- `pyproject.toml` — add `rawpy`, `numpy` deps
- `config/defaults/editing.json` — add `correction` knobs
- `src/shopsteward/editing/config.py` — look/prompt paths + correction-knob loader
- `src/shopsteward/editing/models.py` — `CorrectionSettings`, `EditReport`
- `src/shopsteward/editing/ingest.py` — `require_jpeg` flag (RAW-only support)
- `src/shopsteward/editing/cli.py` — `edit` command

Shared shape: `_shared_helpers` for the seed/last-write-wins pattern is intentionally NOT extracted (looks and presets differ in model shape; a helper would be one indirection for two callers — YAGNI). `looks.py` mirrors `presets.py` structure directly.

---

## Task 1: Dependencies + config knobs + seed assets

**Files:**
- Modify: `pyproject.toml` (dependencies list)
- Modify: `config/defaults/editing.json`
- Modify: `src/shopsteward/editing/config.py`
- Create: `config/defaults/looks/bright-and-true.json`
- Create: `config/defaults/looks/national-geographic.json`
- Create: `config/defaults/prompts/look_profile.txt`
- Test: `tests/editing/test_config_knobs.py`

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add to the `dependencies` list (after `"boto3>=1.34",`):

```toml
  "rawpy>=0.22",
  "numpy>=1.26",
```

- [ ] **Step 2: Sync and verify the native decode works**

Run: `uv sync`
Then: `uv run python -c "import rawpy, numpy; print(rawpy.__version__, numpy.__version__)"`
Expected: prints versions with no ImportError.

Manual smoke (operator, one-time — not committed): decode a real CR3 to confirm libraw supports it:
`uv run python -c "import rawpy; r=rawpy.imread(r'PATH/TO/sample.CR3'); print(r.postprocess().shape, r.camera_whitebalance)"`
Expected: an `(H, W, 3)` shape and 4 WB multipliers. If CR3 fails, stop and report before continuing.

- [ ] **Step 3: Add correction knobs to editing.json**

Replace `config/defaults/editing.json` with:

```json
{
  "naming_template": "{event}-{seq:04}",
  "event_output_root": "data/deliveries",
  "jpeg_quality": 92,
  "correction": {
    "exposure_target_luma": 0.45,
    "exposure_max_stops": 1.5,
    "shadow_trigger_luma": 0.12,
    "shadow_lift_max": 1.0,
    "shadow_range_low": 0,
    "shadow_range_high": 45,
    "cast_trigger": 0.06,
    "cast_nudge_cap": 8,
    "apply_wb_nudge": false
  }
}
```

- [ ] **Step 4: Add seed looks**

`config/defaults/looks/bright-and-true.json`:

```json
{
  "name": "bright-and-true",
  "description": "Clean, faithful color with a gentle lift. Minimal grade.",
  "contrast": 8,
  "tone_curve": [[0, 0], [64, 60], [128, 132], [192, 200], [255, 255]],
  "hsl": {},
  "split_toning": {},
  "vibrance": 10,
  "saturation": 0
}
```

`config/defaults/looks/national-geographic.json`:

```json
{
  "name": "national-geographic",
  "description": "Rich earthy contrast, warm shadows, restrained saturation.",
  "contrast": 18,
  "tone_curve": [[0, 6], [64, 56], [128, 128], [192, 202], [255, 250]],
  "hsl": {"SaturationOrange": 8, "SaturationGreen": -6, "LuminanceBlue": -8},
  "split_toning": {"SplitToningShadowHue": 45, "SplitToningShadowSaturation": 12, "SplitToningBalance": -10},
  "vibrance": 14,
  "saturation": -4
}
```

- [ ] **Step 5: Add the look prompt template**

`config/defaults/prompts/look_profile.txt`:

```
You are a color grading assistant for a photographer. Translate the described
look into concrete Adobe Camera Raw develop settings. Do NOT set white balance
(temperature/tint) — white balance is handled separately. Express the look only
through contrast, a tone curve, HSL adjustments, split toning, vibrance, and
saturation.

Look description: {description}

Return JSON only. Ranges: contrast/vibrance/saturation in [-100, 100]; tone_curve
is a list of [x, y] points with x and y in [0, 255], strictly increasing x,
starting at x=0 and ending at x=255; hsl keys are ACR names like
"SaturationOrange", "LuminanceBlue", "HueRed" with values in [-100, 100];
split_toning keys are "SplitToningShadowHue" (0-360), "SplitToningShadowSaturation"
(0-100), "SplitToningHighlightHue" (0-360), "SplitToningHighlightSaturation"
(0-100), "SplitToningBalance" (-100-100). Keep the grade tasteful and restrained.
```

- [ ] **Step 6: Add config accessors**

Append to `src/shopsteward/editing/config.py`:

```python
LOOKS_DIR = _REPO_ROOT / "config" / "defaults" / "looks"
LOOK_PROMPT_PATH = _REPO_ROOT / "config" / "defaults" / "prompts" / "look_profile.txt"


def load_correction_knobs() -> dict:
    return load_editing_defaults().get("correction", {})


def load_look_prompt() -> str:
    return LOOK_PROMPT_PATH.read_text()
```

- [ ] **Step 7: Write the config test**

`tests/editing/test_config_knobs.py`:

```python
from shopsteward.editing import config


def test_correction_knobs_present_and_typed():
    knobs = config.load_correction_knobs()
    assert knobs["apply_wb_nudge"] is False
    assert knobs["exposure_max_stops"] == 1.5
    assert knobs["shadow_range_high"] == 45


def test_look_prompt_has_description_slot():
    assert "{description}" in config.load_look_prompt()


def test_seed_looks_exist():
    names = {p.stem for p in config.LOOKS_DIR.glob("*.json")}
    assert {"bright-and-true", "national-geographic"} <= names
```

- [ ] **Step 8: Run and commit**

Run: `uv run pytest tests/editing/test_config_knobs.py -v`
Expected: 3 passed.

```bash
git add pyproject.toml config/defaults/editing.json config/defaults/looks config/defaults/prompts/look_profile.txt src/shopsteward/editing/config.py tests/editing/test_config_knobs.py uv.lock
git commit -m "feat(editing): add rawpy/numpy deps, correction knobs, seed looks"
```

---

## Task 2: Data models

**Files:**
- Modify: `src/shopsteward/editing/models.py`
- Create: `src/shopsteward/adapters/look/__init__.py`
- Create: `src/shopsteward/adapters/look/interface.py`
- Test: `tests/adapters/look/test_look_models.py`

- [ ] **Step 1: Write failing model tests**

`tests/adapters/look/test_look_models.py`:

```python
import pytest
from pydantic import ValidationError

from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.models import CorrectionSettings


def test_lookprofile_defaults():
    lp = LookProfile(name="x")
    assert lp.contrast == 0 and lp.tone_curve == [] and lp.vibrance == 0


def test_lookprofile_rejects_out_of_range_contrast():
    with pytest.raises(ValidationError):
        LookProfile(name="x", contrast=500)


def test_correctionsettings_defaults():
    cs = CorrectionSettings()
    assert cs.exposure == 0.0 and cs.shadow_lift == 0.0
    assert cs.temp_nudge == 0 and cs.white_balance == "As Shot"
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/adapters/look/test_look_models.py -v`
Expected: FAIL (ImportError — modules not defined).

- [ ] **Step 3: Add `CorrectionSettings` and `EditReport`**

Append to `src/shopsteward/editing/models.py`:

```python
class CorrectionSettings(BaseModel):
    """Objective per-image correction. WB is trusted as-shot; temp/tint nudges
    are recorded but only written to XMP when apply_wb_nudge is enabled."""

    white_balance: str = "As Shot"
    temp_nudge: int = 0  # recorded recommendation; not written unless apply_wb_nudge
    tint_nudge: int = 0
    exposure: float = 0.0  # stops, global Exposure2012
    shadow_lift: float = 0.0  # local exposure boost in the shadow mask, stops
    shadow_range_low: int = 0  # luminance range mask lower bound, 0-100
    shadow_range_high: int = 45  # upper bound, 0-100


class EditReport(BaseModel):
    edit_job_id: str
    look: str
    processed: int = 0
    written: int = 0
    skipped_existing: int = 0
    failed: int = 0
    sidecar_paths: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Create the look adapter package + interface**

`src/shopsteward/adapters/look/__init__.py`:

```python
"""Look adapter: maps a described look to structured ACR develop settings."""
```

`src/shopsteward/adapters/look/interface.py`:

```python
"""Look adapter protocol. Mirrors adapters.copy: the model owns its own
develop-settings schema; no image and no white balance are ever produced here
(WB is trusted as-shot per the RAW-auto-edit design)."""

from typing import Protocol

from pydantic import BaseModel, Field


class LookProfile(BaseModel):
    name: str
    description: str = ""
    contrast: int = Field(default=0, ge=-100, le=100)
    tone_curve: list[list[int]] = Field(default_factory=list)  # [[x, y], ...]
    hsl: dict[str, int] = Field(default_factory=dict)
    split_toning: dict[str, int] = Field(default_factory=dict)
    vibrance: int = Field(default=0, ge=-100, le=100)
    saturation: int = Field(default=0, ge=-100, le=100)


class LookUsage(BaseModel):
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    est_cost_usd: float | None = None


class LookResult(BaseModel):
    profile: LookProfile
    usage: LookUsage | None = None  # None => fake/fixture mode, no llm.call event


class LookAdapter(Protocol):
    def generate_look(self, description: str, *, model: str) -> LookResult: ...


class LookParseError(RuntimeError):
    """Raised when a look-provider response cannot be parsed into a LookProfile."""
```

- [ ] **Step 5: Run to confirm pass**

Run: `uv run pytest tests/adapters/look/test_look_models.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/shopsteward/editing/models.py src/shopsteward/adapters/look tests/adapters/look/test_look_models.py
git commit -m "feat(editing): add CorrectionSettings, EditReport, LookProfile models"
```

---

## Task 3: RAW decode seam

**Files:**
- Create: `src/shopsteward/editing/rawdecode.py`
- Test: `tests/editing/test_rawdecode.py`

- [ ] **Step 1: Write failing test (fake decoder + protocol conformance)**

`tests/editing/test_rawdecode.py`:

```python
import numpy as np

from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder


def test_fake_decoder_returns_decoded_image():
    img = np.zeros((4, 4, 3), dtype=np.float32)
    dec = FakeRawDecoder({"a.CR3": DecodedImage(rgb=img, wb_multipliers=(2.0, 1.0, 1.5, 0.0), exif={"Model": "R5"})})
    out = dec.decode("a.CR3")
    assert out.rgb.shape == (4, 4, 3)
    assert out.wb_multipliers[0] == 2.0
    assert out.exif["Model"] == "R5"
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/editing/test_rawdecode.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the seam**

`src/shopsteward/editing/rawdecode.py`:

```python
"""RAW decode seam. Real decode uses rawpy (libraw); tests use FakeRawDecoder
so no RAW files are committed (CLAUDE.md hard guardrail)."""

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

# Downscale target for analysis; WB/exposure/shadow stats are stable well below
# full resolution and this keeps decode fast. ponytail: fixed longest-edge box,
# revisit only if estimates prove noisy.
_ANALYSIS_LONG_EDGE = 1024


@dataclass
class DecodedImage:
    rgb: np.ndarray  # HxWx3, float32 in [0, 1]
    wb_multipliers: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 0.0)
    exif: dict = field(default_factory=dict)


class RawDecoder(Protocol):
    def decode(self, raw_path: str) -> DecodedImage: ...


class RawpyDecoder:
    def decode(self, raw_path: str) -> DecodedImage:
        import rawpy  # local import: keeps the native dep out of the import graph for fakes

        with rawpy.imread(raw_path) as raw:
            wb = tuple(float(x) for x in raw.camera_whitebalance[:4])
            rgb16 = raw.postprocess(
                output_bps=16, no_auto_bright=True, use_camera_wb=True
            )
        rgb = rgb16.astype(np.float32) / 65535.0
        rgb = _downscale(rgb, _ANALYSIS_LONG_EDGE)
        return DecodedImage(rgb=rgb, wb_multipliers=wb, exif={})


class FakeRawDecoder:
    """Maps raw_path -> DecodedImage for tests."""

    def __init__(self, images: dict[str, DecodedImage]):
        self._images = images

    def decode(self, raw_path: str) -> DecodedImage:
        if raw_path not in self._images:
            raise FileNotFoundError(raw_path)
        return self._images[raw_path]


def _downscale(rgb: np.ndarray, long_edge: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    scale = long_edge / max(h, w)
    if scale >= 1.0:
        return rgb
    new_h, new_w = max(1, int(h * scale)), max(1, int(w * scale))
    ys = (np.linspace(0, h - 1, new_h)).astype(int)
    xs = (np.linspace(0, w - 1, new_w)).astype(int)
    return rgb[np.ix_(ys, xs)]
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/editing/test_rawdecode.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shopsteward/editing/rawdecode.py tests/editing/test_rawdecode.py
git commit -m "feat(editing): add RAW decode seam (rawpy impl + fake)"
```

---

## Task 4: Correction engine (analyze.py)

**Files:**
- Create: `src/shopsteward/editing/analyze.py`
- Test: `tests/editing/test_analyze.py`

The engine is pure: `DecodedImage` + knobs -> `CorrectionSettings`. Exposure pushes median luma toward the target (capped); shadow lift fires when the dark-region luma is below the trigger; the cast nudge is computed (green–magenta axis, capped) but recorded only — it is never written unless `apply_wb_nudge` is enabled downstream.

- [ ] **Step 1: Write failing tests (synthetic arrays)**

`tests/editing/test_analyze.py`:

```python
import numpy as np

from shopsteward.editing.analyze import analyze_raw, average_corrections
from shopsteward.editing.rawdecode import DecodedImage

KNOBS = {
    "exposure_target_luma": 0.45,
    "exposure_max_stops": 1.5,
    "shadow_trigger_luma": 0.12,
    "shadow_lift_max": 1.0,
    "shadow_range_low": 0,
    "shadow_range_high": 45,
    "cast_trigger": 0.06,
    "cast_nudge_cap": 8,
}


def _flat(value, cast=(1.0, 1.0, 1.0)):
    img = np.zeros((16, 16, 3), dtype=np.float32)
    img[:] = np.array(value, dtype=np.float32) * np.array(cast, dtype=np.float32)
    return DecodedImage(rgb=np.clip(img, 0, 1))


def test_dark_image_gets_positive_exposure():
    cs = analyze_raw(_flat(0.15), KNOBS)
    assert cs.exposure > 0


def test_bright_image_gets_negative_exposure():
    cs = analyze_raw(_flat(0.8), KNOBS)
    assert cs.exposure < 0


def test_exposure_is_capped():
    cs = analyze_raw(_flat(0.001), KNOBS)
    assert cs.exposure <= KNOBS["exposure_max_stops"]


def test_very_dark_triggers_shadow_lift():
    cs = analyze_raw(_flat(0.05), KNOBS)
    assert cs.shadow_lift > 0
    assert cs.shadow_range_high == 45


def test_midtone_image_no_shadow_lift():
    cs = analyze_raw(_flat(0.5), KNOBS)
    assert cs.shadow_lift == 0.0


def test_green_cast_nudge_is_capped_and_recorded():
    cs = analyze_raw(_flat(0.4, cast=(0.85, 1.15, 0.85)), KNOBS)
    assert cs.tint_nudge != 0
    assert abs(cs.tint_nudge) <= KNOBS["cast_nudge_cap"]


def test_neutral_image_no_cast_nudge():
    cs = analyze_raw(_flat(0.4), KNOBS)
    assert cs.tint_nudge == 0


def test_average_corrections_means_exposure():
    a = analyze_raw(_flat(0.15), KNOBS)
    b = analyze_raw(_flat(0.75), KNOBS)
    avg = average_corrections([a, b])
    assert min(a.exposure, b.exposure) <= avg.exposure <= max(a.exposure, b.exposure)
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/editing/test_analyze.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement analyze.py**

`src/shopsteward/editing/analyze.py`:

```python
"""Pure objective-correction engine. Input: a decoded RAW + calibration knobs.
Output: CorrectionSettings. No I/O, no rawpy, no XMP — deterministic and unit-
testable on synthetic arrays."""

import math

import numpy as np

from shopsteward.editing.models import CorrectionSettings

# Rec. 709 luma weights.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ _LUMA


def analyze_raw(decoded, knobs: dict) -> CorrectionSettings:
    rgb = np.clip(decoded.rgb.astype(np.float32), 0.0, 1.0)
    luma = _luma(rgb)

    exposure = _exposure(luma, knobs)
    shadow_lift, lo, hi = _shadow(luma, knobs)
    temp_nudge, tint_nudge = _cast_nudge(rgb, knobs)

    return CorrectionSettings(
        exposure=exposure,
        shadow_lift=shadow_lift,
        shadow_range_low=lo,
        shadow_range_high=hi,
        temp_nudge=temp_nudge,
        tint_nudge=tint_nudge,
    )


def _exposure(luma: np.ndarray, knobs: dict) -> float:
    target = float(knobs["exposure_target_luma"])
    cap = float(knobs["exposure_max_stops"])
    median = float(np.median(luma))
    if median <= 1e-4:
        return cap
    stops = math.log2(target / median)
    return round(max(-cap, min(cap, stops)), 2)


def _shadow(luma: np.ndarray, knobs: dict) -> tuple[float, int, int]:
    trigger = float(knobs["shadow_trigger_luma"])
    lift_max = float(knobs["shadow_lift_max"])
    lo = int(knobs["shadow_range_low"])
    hi = int(knobs["shadow_range_high"])
    # Mean luma of the darkest quartile as the shadow proxy.
    dark = luma[luma <= np.quantile(luma, 0.25)]
    dark_mean = float(dark.mean()) if dark.size else float(luma.mean())
    if dark_mean >= trigger:
        return 0.0, lo, hi
    # Deeper shadows -> more lift, scaled to the cap.
    deficit = (trigger - dark_mean) / trigger
    lift = round(min(lift_max, lift_max * deficit), 2)
    return lift, lo, hi


def _cast_nudge(rgb: np.ndarray, knobs: dict) -> tuple[int, int]:
    """Green-magenta (tint) cast estimate only. Temp axis needs Kelvin
    calibration and is left at 0. Recorded for downstream; only written when
    apply_wb_nudge is enabled. ponytail: tint-axis proxy, upgrade to per-camera
    calibration if the nudge is ever enabled by default."""
    trigger = float(knobs["cast_trigger"])
    cap = int(knobs["cast_nudge_cap"])
    r, g, b = (float(rgb[..., i].mean()) for i in range(3))
    gray = (r + g + b) / 3.0
    if gray <= 1e-4:
        return 0, 0
    # Green excess relative to red/blue average -> positive means push toward magenta.
    green_bias = (g - (r + b) / 2.0) / gray
    if abs(green_bias) < trigger:
        return 0, 0
    tint_nudge = int(round(max(-cap, min(cap, green_bias * cap / 0.2))))
    return 0, tint_nudge


def average_corrections(items: list[CorrectionSettings]) -> CorrectionSettings:
    """Batch/sequence lock: mean of continuous corrections applied to all frames."""
    n = len(items)
    if n == 0:
        return CorrectionSettings()
    return CorrectionSettings(
        exposure=round(sum(c.exposure for c in items) / n, 2),
        shadow_lift=round(sum(c.shadow_lift for c in items) / n, 2),
        shadow_range_low=items[0].shadow_range_low,
        shadow_range_high=items[0].shadow_range_high,
        temp_nudge=int(round(sum(c.temp_nudge for c in items) / n)),
        tint_nudge=int(round(sum(c.tint_nudge for c in items) / n)),
    )
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/editing/test_analyze.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shopsteward/editing/analyze.py tests/editing/test_analyze.py
git commit -m "feat(editing): add pure correction engine (exposure, shadow, cast nudge)"
```

---

## Task 5: XMP compose + write

**Files:**
- Create: `src/shopsteward/editing/xmp.py`
- Test: `tests/editing/test_xmp.py`

`compose` builds an ACR sidecar string. WB is always `As Shot` (Temp/Tint omitted) unless `apply_wb_nudge` is passed true AND a baseline is known — v1 never passes true, so WB overrides are not emitted. Correction owns `Exposure2012` and the local shadow-lift mask; the look owns everything else. `write_sidecar` writes `<raw>.xmp`, honoring `overwrite`.

- [ ] **Step 1: Write failing tests**

`tests/editing/test_xmp.py`:

```python
import xml.etree.ElementTree as ET
from pathlib import Path

from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.models import CorrectionSettings
from shopsteward.editing.xmp import compose, sidecar_path, write_sidecar

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"


def _parse(xmp: str) -> ET.Element:
    return ET.fromstring(xmp)  # raises on malformed XML


def test_compose_is_wellformed_and_wb_as_shot():
    xmp = compose(CorrectionSettings(exposure=0.5), LookProfile(name="x"))
    root = _parse(xmp)
    desc = root.find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc.get(f"{{{CRS}}}WhiteBalance") == "As Shot"
    assert f"{{{CRS}}}Temperature" not in desc.attrib
    assert desc.get(f"{{{CRS}}}Exposure2012") == "0.50"


def test_look_owns_contrast_not_correction():
    xmp = compose(CorrectionSettings(), LookProfile(name="x", contrast=18, vibrance=14))
    desc = _parse(xmp).find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc.get(f"{{{CRS}}}Contrast2012") == "18"
    assert desc.get(f"{{{CRS}}}Vibrance") == "14"


def test_shadow_lift_emits_local_range_mask():
    xmp = compose(CorrectionSettings(shadow_lift=0.8, shadow_range_high=45), LookProfile(name="x"))
    assert "MaskGroupBasedCorrections" in xmp
    assert "RangeMask" in xmp or "Luminance" in xmp


def test_no_shadow_lift_omits_mask():
    xmp = compose(CorrectionSettings(shadow_lift=0.0), LookProfile(name="x"))
    assert "MaskGroupBasedCorrections" not in xmp


def test_contrast_is_clamped():
    xmp = compose(CorrectionSettings(), LookProfile.model_construct(name="x", contrast=999,
        tone_curve=[], hsl={}, split_toning={}, vibrance=0, saturation=0))
    desc = _parse(xmp).find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc.get(f"{{{CRS}}}Contrast2012") == "100"


def test_write_sidecar_creates_file_and_skips_existing(tmp_path: Path):
    raw = tmp_path / "IMG_1.CR3"
    raw.write_bytes(b"stub")
    xmp = compose(CorrectionSettings(), LookProfile(name="x"))
    assert write_sidecar(raw, xmp, overwrite=False) is True
    assert sidecar_path(raw).exists()
    assert write_sidecar(raw, xmp, overwrite=False) is False  # already exists
    assert write_sidecar(raw, xmp, overwrite=True) is True
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/editing/test_xmp.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement xmp.py**

`src/shopsteward/editing/xmp.py`:

```python
"""Compose an Adobe Camera Raw XMP sidecar from a correction + a look, and write
it next to the RAW. WB is trusted as-shot (Temp/Tint omitted). Correction owns
Exposure2012 + a local luminance-range shadow-lift mask; the look owns Contrast,
tone curve, HSL, split toning, vibrance, saturation.

ponytail: the local-mask (MaskGroupBasedCorrections) block is the fiddliest part
of the ACR schema; it is verified structurally in tests and MUST be confirmed by
opening one sidecar in Lightroom during the Task 1 smoke before trusting output.
"""

from pathlib import Path

from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.models import CorrectionSettings

_CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def sidecar_path(raw_path: Path) -> Path:
    return raw_path.with_suffix(".xmp")


def compose(correction: CorrectionSettings, look: LookProfile) -> str:
    attrs: list[str] = [
        'crs:Version="15.0"',
        'crs:WhiteBalance="As Shot"',
        f'crs:Exposure2012="{correction.exposure:.2f}"',
        f'crs:Contrast2012="{_clamp(look.contrast, -100, 100)}"',
        f'crs:Vibrance="{_clamp(look.vibrance, -100, 100)}"',
        f'crs:Saturation="{_clamp(look.saturation, -100, 100)}"',
    ]
    for key, value in sorted(look.hsl.items()):
        attrs.append(f'crs:{_xml_name(key)}="{_clamp(int(value), -100, 100)}"')
    for key, value in sorted(look.split_toning.items()):
        attrs.append(f'crs:{_xml_name(key)}="{int(value)}"')

    children: list[str] = []
    if look.tone_curve:
        children.append(_tone_curve(look.tone_curve))
    if correction.shadow_lift > 0:
        children.append(_shadow_mask(correction))

    attr_block = "\n    ".join(attrs)
    child_block = "\n".join(children)
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="ShopSteward">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        f'    xmlns:crs="{_CRS}"\n'
        f"    {attr_block}>\n"
        f"{child_block}\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
    )


def _tone_curve(points: list[list[int]]) -> str:
    lis = "".join(
        f"     <rdf:li>{int(x)}, {int(y)}</rdf:li>\n" for x, y in points
    )
    return (
        "   <crs:ToneCurvePV2012>\n    <rdf:Seq>\n"
        f"{lis}"
        "    </rdf:Seq>\n   </crs:ToneCurvePV2012>"
    )


def _shadow_mask(correction: CorrectionSettings) -> str:
    lo = _clamp(correction.shadow_range_low, 0, 100)
    hi = _clamp(correction.shadow_range_high, 0, 100)
    return (
        "   <crs:MaskGroupBasedCorrections>\n"
        "    <rdf:Seq>\n"
        "     <rdf:li>\n"
        "      <rdf:Description\n"
        f'       crs:LocalExposure2012="{correction.shadow_lift:.2f}"\n'
        '       crs:CorrectionActive="true">\n'
        "       <crs:CorrectionMasks>\n        <rdf:Seq>\n"
        "         <rdf:li>\n"
        '          <rdf:Description crs:What="Mask/RangeMask" crs:MaskActive="true"\n'
        '           crs:MaskName="Shadows" crs:MaskBlendMode="0" crs:RangeType="Luminance"\n'
        f'           crs:LumRangeLower="{lo / 100:.3f}" crs:LumRangeUpper="{hi / 100:.3f}"/>\n'
        "         </rdf:li>\n"
        "        </rdf:Seq>\n       </crs:CorrectionMasks>\n"
        "      </rdf:Description>\n"
        "     </rdf:li>\n"
        "    </rdf:Seq>\n"
        "   </crs:MaskGroupBasedCorrections>"
    )


def _xml_name(key: str) -> str:
    # Keys come from our own config/LLM schema; strip anything not a safe XML name char.
    return "".join(c for c in key if c.isalnum())


def write_sidecar(raw_path: Path, xmp: str, *, overwrite: bool) -> bool:
    target = sidecar_path(raw_path)
    if target.exists() and not overwrite:
        return False
    target.write_text(xmp, encoding="utf-8")
    return True
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/editing/test_xmp.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shopsteward/editing/xmp.py tests/editing/test_xmp.py
git commit -m "feat(editing): compose+write ACR XMP sidecar (WB as-shot, local shadow mask)"
```

---

## Task 6: Look adapter (OpenRouter + fakes)

**Files:**
- Create: `src/shopsteward/adapters/look/openrouter.py`
- Create: `src/shopsteward/adapters/look/fake.py`
- Test: `tests/adapters/look/test_look_adapter.py`

- [ ] **Step 1: Write failing tests**

`tests/adapters/look/test_look_adapter.py`:

```python
import httpx
import pytest
import respx

from shopsteward.adapters.look.fake import FakeLookAdapter, FixtureLookAdapter
from shopsteward.adapters.look.interface import LookParseError, LookProfile, LookResult
from shopsteward.adapters.look.openrouter import BASE, OpenRouterLookAdapter


def test_fixture_adapter_is_deterministic():
    a = FixtureLookAdapter().generate_look("cinematic mexico", model="m")
    b = FixtureLookAdapter().generate_look("cinematic mexico", model="m")
    assert a.profile.model_dump() == b.profile.model_dump()
    assert a.usage is None


def test_fake_adapter_replays_queue():
    queued = LookResult(profile=LookProfile(name="q", contrast=5))
    fake = FakeLookAdapter([queued])
    assert fake.generate_look("x", model="m").profile.contrast == 5
    with pytest.raises(RuntimeError):
        fake.generate_look("x", model="m")


@respx.mock
def test_openrouter_parses_profile():
    payload = {
        "choices": [{"message": {"content": '{"contrast": 12, "tone_curve": [[0,0],[255,255]], "hsl": {}, "split_toning": {}, "vibrance": 8, "saturation": 0}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    respx.post(BASE).mock(return_value=httpx.Response(200, json=payload))
    adapter = OpenRouterLookAdapter(api_key="k", prompt_template="{description}")
    result = adapter.generate_look("warm and moody", model="test/model")
    assert result.profile.contrast == 12
    assert result.profile.name == "warm and moody"
    assert result.usage.output_tokens == 20


@respx.mock
def test_openrouter_raises_on_bad_json():
    payload = {"choices": [{"message": {"content": "not json"}}]}
    respx.post(BASE).mock(return_value=httpx.Response(200, json=payload))
    adapter = OpenRouterLookAdapter(api_key="k", prompt_template="{description}")
    with pytest.raises(LookParseError):
        adapter.generate_look("x", model="m")
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/adapters/look/test_look_adapter.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement fake.py**

`src/shopsteward/adapters/look/fake.py`:

```python
"""Fixture-backed and programmable fake look adapters for tests
(adapters.copy.fake precedent)."""

import hashlib

from shopsteward.adapters.look.interface import LookProfile, LookResult


class FixtureLookAdapter:
    """Deterministic pseudo-look derived from the description. usage=None => no
    llm.call event (offline default)."""

    def generate_look(self, description: str, *, model: str) -> LookResult:
        digest = hashlib.sha256(description.encode()).hexdigest()
        contrast = int(digest[:2], 16) % 40 - 10  # -10..29, stable per description
        vibrance = int(digest[2:4], 16) % 30
        profile = LookProfile(
            name=description,
            description=f"fixture look for {description!r}",
            contrast=contrast,
            tone_curve=[[0, 0], [128, 128], [255, 255]],
            vibrance=vibrance,
        )
        return LookResult(profile=profile, usage=None)


class FakeLookAdapter:
    """Programmable queue (results + exceptions) for ledger/parse-failure tests."""

    def __init__(self, results: list[LookResult | Exception]):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    def generate_look(self, description: str, *, model: str) -> LookResult:
        self.calls.append((description, model))
        if not self._results:
            raise RuntimeError("FakeLookAdapter exhausted: no more queued results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
```

- [ ] **Step 4: Implement openrouter.py**

`src/shopsteward/adapters/look/openrouter.py`:

```python
"""OpenRouter look adapter. httpx only — no vendor SDK (adapters.copy.openrouter
precedent). NOT wired to any default path; live use is gated (flag + env + key)
by the caller. Text-only: only the look description is sent, never a photograph."""

import json

import httpx
from pydantic import ValidationError

from shopsteward.adapters.look.interface import (
    LookParseError,
    LookProfile,
    LookResult,
    LookUsage,
)

BASE = "https://openrouter.ai/api/v1/chat/completions"

_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "contrast": {"type": "integer", "minimum": -100, "maximum": 100},
        "tone_curve": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        },
        "hsl": {"type": "object", "additionalProperties": {"type": "integer"}},
        "split_toning": {"type": "object", "additionalProperties": {"type": "integer"}},
        "vibrance": {"type": "integer", "minimum": -100, "maximum": 100},
        "saturation": {"type": "integer", "minimum": -100, "maximum": 100},
    },
    "required": ["contrast", "tone_curve", "hsl", "split_toning", "vibrance", "saturation"],
    "additionalProperties": False,
}

_MAX_ERROR_LEN = 500


class OpenRouterLookAdapter:
    def __init__(
        self,
        api_key: str,
        prompt_template: str,
        pricing: dict[str, dict[str, float]] | None = None,
        temperature: float = 0.7,
        timeout: float = 60.0,
    ):
        self._prompt_template = prompt_template
        self._pricing = pricing
        self._temperature = temperature
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/epd11183/shopsteward",
                "X-Title": "ShopSteward",
            },
            timeout=timeout,
        )

    def generate_look(self, description: str, *, model: str) -> LookResult:
        prompt = self._prompt_template.format(description=description)
        body = {
            "model": model,
            "temperature": self._temperature,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "look_profile", "strict": True, "schema": _PROFILE_SCHEMA},
            },
        }
        resp = self._client.post(BASE, json=body)
        resp.raise_for_status()
        payload = resp.json()

        try:
            text = payload["choices"][0]["message"]["content"]
            data = json.loads(text)
            data["name"] = description
            data["description"] = description
            profile = LookProfile.model_validate(data)
        except (KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raise LookParseError(
                f"could not parse OpenRouter response: {payload!r:.{_MAX_ERROR_LEN}}"
            ) from exc

        return LookResult(profile=profile, usage=self._build_usage(payload, model))

    def _build_usage(self, payload: dict, model: str) -> LookUsage:
        meta = payload.get("usage", {})
        input_tokens = meta.get("prompt_tokens")
        output_tokens = meta.get("completion_tokens")
        est_cost_usd = None
        have = input_tokens is not None and output_tokens is not None
        if self._pricing and model in self._pricing and have:
            rates = self._pricing[model]
            est_cost_usd = (input_tokens / 1e6) * rates["in"] + (output_tokens / 1e6) * rates["out"]
        return LookUsage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=est_cost_usd,
        )
```

- [ ] **Step 5: Run to confirm pass**

Run: `uv run pytest tests/adapters/look/test_look_adapter.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/shopsteward/adapters/look/openrouter.py src/shopsteward/adapters/look/fake.py tests/adapters/look/test_look_adapter.py
git commit -m "feat(look): add OpenRouter look adapter + fakes"
```

---

## Task 7: Look store + resolution (looks.py)

**Files:**
- Create: `src/shopsteward/editing/looks.py`
- Test: `tests/editing/test_looks.py`

Mirrors `presets.py` (event-sourced, last-write-wins by name), plus described-look resolution keyed by normalized description.

- [ ] **Step 1: Write failing tests**

`tests/editing/test_looks.py`:

```python
from shopsteward.adapters.look.fake import FakeLookAdapter, FixtureLookAdapter
from shopsteward.adapters.look.interface import LookProfile, LookResult
from shopsteward.core.db import connect, migrate
from shopsteward.editing import looks
from shopsteward.editing.config import LOOKS_DIR

USER = 1


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def test_seed_and_get():
    c = _conn()
    n = looks.seed(c, USER, LOOKS_DIR)
    assert n >= 2
    lp = looks.get_look(c, USER, "national-geographic")
    assert lp.contrast == 18


def test_seed_is_idempotent():
    c = _conn()
    looks.seed(c, USER, LOOKS_DIR)
    assert looks.seed(c, USER, LOOKS_DIR) == 0


def test_save_is_last_write_wins():
    c = _conn()
    looks.save_look(c, USER, LookProfile(name="x", contrast=1))
    looks.save_look(c, USER, LookProfile(name="x", contrast=9))
    assert looks.get_look(c, USER, "x").contrast == 9


def test_resolve_named_look_does_not_call_llm():
    c = _conn()
    looks.seed(c, USER, LOOKS_DIR)
    adapter = FakeLookAdapter([])  # would raise if called
    lp = looks.resolve_look(c, USER, "bright-and-true", adapter, model="m", regenerate=False)
    assert lp.name == "bright-and-true"


def test_resolve_description_generates_then_reloads():
    c = _conn()
    adapter = FixtureLookAdapter()
    first = looks.resolve_look(c, USER, "cinematic mexico", adapter, model="m", regenerate=False)
    # Second call must NOT regenerate: use a queue-empty fake to prove no LLM call.
    reload_adapter = FakeLookAdapter([])
    again = looks.resolve_look(c, USER, "cinematic mexico", reload_adapter, model="m", regenerate=False)
    assert again.model_dump() == first.model_dump()


def test_regenerate_forces_new_call():
    c = _conn()
    looks.resolve_look(c, USER, "cinematic mexico", FixtureLookAdapter(), model="m", regenerate=False)
    forced = LookResult(profile=LookProfile(name="forced", contrast=77))
    out = looks.resolve_look(c, USER, "cinematic mexico", FakeLookAdapter([forced]), model="m", regenerate=True)
    assert out.contrast == 77
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/editing/test_looks.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement looks.py**

`src/shopsteward/editing/looks.py`:

```python
"""Event-sourced look store (presets.py precedent) + described-look resolution.
Named looks seed from config/defaults/looks/*.json; described looks are keyed by
normalized description so an identical phrase reloads instead of regenerating."""

import hashlib
import json
import sqlite3
from pathlib import Path

from shopsteward.adapters.look.interface import LookAdapter, LookProfile
from shopsteward.core.events import Event, append, read_all

LOOK_EVENT_TYPES = ("look.seeded", "look.updated")


def _latest_by_name(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for e in read_all(conn, "look."):
        if e.user_id != user_id or e.type not in LOOK_EVENT_TYPES:
            continue
        latest[e.payload["name"]] = e.payload
    return latest


def _profile_from_payload(payload: dict) -> LookProfile:
    return LookProfile.model_validate(payload["profile"])


def seed(conn: sqlite3.Connection, user_id: int, defaults_dir: Path) -> int:
    existing = _latest_by_name(conn, user_id)
    seeded = 0
    for path in sorted(Path(defaults_dir).glob("*.json")):
        profile = LookProfile.model_validate(json.loads(path.read_text()))
        prior = existing.get(profile.name)
        if prior is not None and prior.get("profile") == profile.model_dump():
            continue
        append(conn, Event(user_id=user_id, type="look.seeded",
                           payload={"name": profile.name, "profile": profile.model_dump(),
                                    "source": "defaults"}))
        seeded += 1
    return seeded


def list_looks(conn: sqlite3.Connection, user_id: int) -> list[LookProfile]:
    return [_profile_from_payload(p) for _, p in sorted(_latest_by_name(conn, user_id).items())]


def get_look(conn: sqlite3.Connection, user_id: int, name: str) -> LookProfile:
    latest = _latest_by_name(conn, user_id)
    payload = latest.get(name)
    if payload is None:
        available = ", ".join(sorted(latest)) or "(none seeded)"
        raise KeyError(f"unknown look '{name}'; available: {available}")
    return _profile_from_payload(payload)


def save_look(conn: sqlite3.Connection, user_id: int, profile: LookProfile) -> None:
    append(conn, Event(user_id=user_id, type="look.updated",
                       payload={"name": profile.name, "profile": profile.model_dump(),
                                "source": "generated"}))


def _desc_key(description: str) -> str:
    normalized = " ".join(description.lower().split())
    return "desc:" + hashlib.sha256(normalized.encode()).hexdigest()[:12]


def resolve_look(
    conn: sqlite3.Connection,
    user_id: int,
    look_arg: str,
    adapter: LookAdapter,
    *,
    model: str,
    regenerate: bool,
) -> LookProfile:
    """Resolve --look: an exact stored name wins; otherwise treat as a description
    keyed by normalized text (reload unless --regenerate); else generate + save."""
    latest = _latest_by_name(conn, user_id)
    if look_arg in latest:
        return _profile_from_payload(latest[look_arg])

    key = _desc_key(look_arg)
    if not regenerate and key in latest:
        return _profile_from_payload(latest[key])

    result = adapter.generate_look(look_arg, model=model)
    profile = result.profile.model_copy(update={"name": key, "description": look_arg})
    save_look(conn, user_id, profile)
    return profile
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/editing/test_looks.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shopsteward/editing/looks.py tests/editing/test_looks.py
git commit -m "feat(editing): event-sourced look store + described-look resolution"
```

---

## Task 8: RAW-only ingestion

**Files:**
- Modify: `src/shopsteward/editing/ingest.py`
- Test: `tests/editing/test_ingest_rawonly.py`

The edit path decodes RAWs directly and needs no JPEG. Add `require_jpeg: bool = True`; when false, a RAW without a JPEG sibling is ingested (jpeg_path=None, exif={}) instead of being logged unpaired.

- [ ] **Step 1: Write failing test**

`tests/editing/test_ingest_rawonly.py`:

```python
from pathlib import Path

from shopsteward.core.db import connect, migrate
from shopsteward.editing.ingest import ingest_folder

USER = 1


def test_raw_only_ingested_when_jpeg_not_required(tmp_path: Path):
    (tmp_path / "IMG_1.CR3").write_bytes(b"raw-bytes-1")
    (tmp_path / "IMG_2.CR3").write_bytes(b"raw-bytes-2")
    conn = connect(":memory:")
    migrate(conn)
    report = ingest_folder(conn, USER, tmp_path, mode="mass", require_jpeg=False)
    assert report.paired == 2
    assert report.unpaired == 0


def test_raw_only_still_unpaired_when_jpeg_required(tmp_path: Path):
    (tmp_path / "IMG_1.CR3").write_bytes(b"raw-bytes-1")
    conn = connect(":memory:")
    migrate(conn)
    report = ingest_folder(conn, USER, tmp_path, mode="mass", require_jpeg=True)
    assert report.paired == 0
    assert report.unpaired == 1
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/editing/test_ingest_rawonly.py -v`
Expected: FAIL (TypeError: unexpected keyword `require_jpeg`).

- [ ] **Step 3: Modify ingest_folder**

In `src/shopsteward/editing/ingest.py`, add the parameter to the signature (after `output_folder`):

```python
    output_folder: str | None = None,
    require_jpeg: bool = True,
```

Then replace the `if jpeg_path is None:` block (lines ~129-143) with:

```python
        if jpeg_path is None and require_jpeg:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="photo.unpaired",
                    payload={
                        "ingest_job_id": ingest_job_id,
                        "path": str(raw_path),
                        "reason": "missing_jpeg",
                    },
                ),
            )
            unpaired += 1
            continue
```

And change the EXIF line (currently `exif = _extract_exif(jpeg_path)`) to:

```python
        exif = _extract_exif(jpeg_path) if jpeg_path is not None else {}
```

And make the ingested payload tolerate a missing JPEG — change `"jpeg_path": str(jpeg_path),` to:

```python
                    "jpeg_path": str(jpeg_path) if jpeg_path is not None else None,
```

- [ ] **Step 4: Run to confirm pass (and no regression)**

Run: `uv run pytest tests/editing/test_ingest_rawonly.py -v`
Expected: 2 passed.
Run: `uv run pytest tests/editing/ -k ingest -v`
Expected: existing ingest tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/shopsteward/editing/ingest.py tests/editing/test_ingest_rawonly.py
git commit -m "feat(editing): support RAW-only ingestion (require_jpeg flag)"
```

---

## Task 9: Orchestration (edit.py)

**Files:**
- Create: `src/shopsteward/editing/edit.py`
- Test: `tests/editing/test_edit.py`

`run_edit` ties it together: resolve the look first (fail fast before any file write), ingest RAW-only, then per photo decode → analyze → compose → write, emitting events and returning an `EditReport`. `--wb-lock` averages corrections across the batch.

- [ ] **Step 1: Write failing tests**

`tests/editing/test_edit.py`:

```python
from pathlib import Path

import numpy as np
import pytest

from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.adapters.look.interface import LookParseError
from shopsteward.core.db import connect, migrate
from shopsteward.editing.edit import run_edit
from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder
from shopsteward.editing.xmp import sidecar_path

USER = 1
KNOBS = {
    "exposure_target_luma": 0.45, "exposure_max_stops": 1.5, "shadow_trigger_luma": 0.12,
    "shadow_lift_max": 1.0, "shadow_range_low": 0, "shadow_range_high": 45,
    "cast_trigger": 0.06, "cast_nudge_cap": 8, "apply_wb_nudge": False,
}


def _folder_with_raws(tmp_path: Path, names: list[str]) -> tuple[Path, FakeRawDecoder]:
    images = {}
    for i, name in enumerate(names):
        raw = tmp_path / name
        raw.write_bytes(b"stub")
        img = np.full((8, 8, 3), 0.1 + 0.1 * i, dtype=np.float32)
        images[str(raw)] = DecodedImage(rgb=img)
    return tmp_path, FakeRawDecoder(images)


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def test_run_edit_writes_a_sidecar_per_raw(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3", "B.CR3"])
    report = run_edit(_conn(), USER, folder, "bright-and-true",
                      decoder=decoder, look_adapter=FixtureLookAdapter(),
                      model="m", knobs=KNOBS, regenerate=False, overwrite=False, wb_lock=False)
    assert report.written == 2
    assert sidecar_path(tmp_path / "A.CR3").exists()
    assert sidecar_path(tmp_path / "B.CR3").exists()


def test_run_edit_skips_existing_without_overwrite(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3"])
    sidecar_path(tmp_path / "A.CR3").write_text("existing")
    report = run_edit(_conn(), USER, folder, "bright-and-true",
                      decoder=decoder, look_adapter=FixtureLookAdapter(),
                      model="m", knobs=KNOBS, regenerate=False, overwrite=False, wb_lock=False)
    assert report.written == 0 and report.skipped_existing == 1


def test_run_edit_fails_fast_on_look_error_before_writing(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3"])

    class Boom:
        def generate_look(self, description, *, model):
            raise LookParseError("boom")

    with pytest.raises(LookParseError):
        run_edit(_conn(), USER, folder, "some new look",
                 decoder=decoder, look_adapter=Boom(),
                 model="m", knobs=KNOBS, regenerate=False, overwrite=False, wb_lock=False)
    assert not sidecar_path(tmp_path / "A.CR3").exists()  # nothing written


def test_wb_lock_applies_same_exposure_to_all(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3", "B.CR3"])
    report = run_edit(_conn(), USER, folder, "bright-and-true",
                      decoder=decoder, look_adapter=FixtureLookAdapter(),
                      model="m", knobs=KNOBS, regenerate=False, overwrite=False, wb_lock=True)
    a = sidecar_path(tmp_path / "A.CR3").read_text()
    b = sidecar_path(tmp_path / "B.CR3").read_text()
    def _exp(x): return x.split('crs:Exposure2012="')[1].split('"')[0]
    assert _exp(a) == _exp(b)
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/editing/test_edit.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement edit.py**

`src/shopsteward/editing/edit.py`:

```python
"""Mass-mode edit orchestration: resolve look (fail fast) -> ingest RAWs ->
per-photo decode/analyze/compose/write sidecar -> events -> EditReport."""

import sqlite3
import uuid
from pathlib import Path

from shopsteward.adapters.look.interface import LookAdapter
from shopsteward.core.events import Event, append
from shopsteward.editing import looks
from shopsteward.editing.analyze import analyze_raw, average_corrections
from shopsteward.editing.ingest import ingest_folder
from shopsteward.editing.models import CorrectionSettings, EditReport
from shopsteward.editing.rawdecode import RawDecoder
from shopsteward.editing.xmp import compose, write_sidecar


def run_edit(
    conn: sqlite3.Connection,
    user_id: int,
    path: Path,
    look_arg: str,
    *,
    decoder: RawDecoder,
    look_adapter: LookAdapter,
    model: str,
    knobs: dict,
    regenerate: bool,
    overwrite: bool,
    wb_lock: bool,
) -> EditReport:
    # 1. Resolve the look FIRST — this may hit the LLM and must fail before any
    #    sidecar is written, so a batch is never half-graded.
    looks.seed(conn, user_id, _looks_dir())
    look = looks.resolve_look(conn, user_id, look_arg, look_adapter, model=model, regenerate=regenerate)

    edit_job_id = str(uuid.uuid4())
    report = EditReport(edit_job_id=edit_job_id, look=look.name)
    append(conn, Event(user_id=user_id, type="editjob.started",
                       payload={"edit_job_id": edit_job_id, "path": str(path), "look": look.name,
                                "wb_lock": wb_lock}))

    ingest = ingest_folder(conn, user_id, path, mode="mass", require_jpeg=False)

    # Resolve raw paths for the freshly-ingested photos.
    raw_paths = _raw_paths_for(conn, user_id, ingest.photo_ids)

    # 2. Decode + analyze every frame (needed up-front for wb-lock averaging).
    decoded = {}
    corrections: dict[str, CorrectionSettings] = {}
    for rp in raw_paths:
        try:
            img = decoder.decode(str(rp))
            decoded[str(rp)] = img
            corrections[str(rp)] = analyze_raw(img, knobs)
        except Exception as exc:  # noqa: BLE001 - decode errors are per-frame, non-fatal
            report.failed += 1
            append(conn, Event(user_id=user_id, type="sidecar.failed",
                               payload={"edit_job_id": edit_job_id, "raw_path": str(rp),
                                        "error": repr(exc)}))

    if wb_lock and corrections:
        locked = average_corrections(list(corrections.values()))
        corrections = {k: locked for k in corrections}

    # 3. Compose + write.
    for rp in raw_paths:
        if str(rp) not in corrections:
            continue  # decode failed above
        report.processed += 1
        xmp = compose(corrections[str(rp)], look)
        if write_sidecar(rp, xmp, overwrite=overwrite):
            report.written += 1
            report.sidecar_paths.append(str(rp.with_suffix(".xmp")))
            append(conn, Event(user_id=user_id, type="sidecar.written",
                               payload={"edit_job_id": edit_job_id, "raw_path": str(rp)}))
        else:
            report.skipped_existing += 1

    append(conn, Event(user_id=user_id, type="editjob.completed",
                       payload={"edit_job_id": edit_job_id, "written": report.written,
                                "skipped_existing": report.skipped_existing, "failed": report.failed}))
    return report


def _looks_dir() -> Path:
    from shopsteward.editing.config import LOOKS_DIR

    return LOOKS_DIR


def _raw_paths_for(conn: sqlite3.Connection, user_id: int, photo_ids: list[str]) -> list[Path]:
    from shopsteward.core.events import read_all

    wanted = set(photo_ids)
    paths: list[Path] = []
    for e in read_all(conn, "photo.ingested"):
        if e.user_id == user_id and e.payload["photo_id"] in wanted:
            paths.append(Path(e.payload["raw_path"]))
    return sorted(paths)
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/editing/test_edit.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/shopsteward/editing/edit.py tests/editing/test_edit.py
git commit -m "feat(editing): run_edit orchestration (fail-fast look, wb-lock, sidecars)"
```

---

## Task 10: CLI wiring

**Files:**
- Modify: `src/shopsteward/editing/cli.py`
- Test: `tests/editing/test_cli_edit.py`

Add `shopsteward edit run <path> --look ...`. The command constructs the offline default adapters (`FixtureLookAdapter`, `RawpyDecoder`); live LLM use stays gated (flag+env+key) and is out of scope here.

- [ ] **Step 1: Write failing test (Typer CliRunner, offline fixture look)**

`tests/editing/test_cli_edit.py`:

```python
from pathlib import Path

from typer.testing import CliRunner

from shopsteward.editing.cli import edit_app

runner = CliRunner()


def test_edit_run_reports_written(tmp_path: Path, monkeypatch):
    # Point the DB at a temp file and stub decode so the CLI runs offline.
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    (tmp_path / "IMG_1.CR3").write_bytes(b"stub")

    import numpy as np

    from shopsteward.editing import cli as cli_mod
    from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

    fake = FakeRawDecoder({str(tmp_path / "IMG_1.CR3"): DecodedImage(rgb=np.full((8, 8, 3), 0.2, np.float32))})
    monkeypatch.setattr(cli_mod, "_default_decoder", lambda: fake)

    result = runner.invoke(edit_app, ["run", str(tmp_path), "--look", "bright-and-true"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output
    assert (tmp_path / "IMG_1.xmp").exists()
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/editing/test_cli_edit.py -v`
Expected: FAIL (no `run` command / no `_default_decoder`).

- [ ] **Step 3: Add the command**

In `src/shopsteward/editing/cli.py`, add imports at the top:

```python
from pathlib import Path

from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.editing.config import load_correction_knobs
from shopsteward.editing.edit import run_edit
from shopsteward.editing.rawdecode import RawpyDecoder
```

Add these helpers (seams the test overrides) and the command after the `status` command:

```python
def _default_decoder():
    return RawpyDecoder()


def _default_look_adapter():
    # Offline default. Live LLM generation is gated (flag+env+key) — out of scope here.
    return FixtureLookAdapter()


@edit_app.command("run")
def run(
    path: Annotated[str, typer.Argument(help="Folder of RAW files to edit")],
    look: Annotated[str, typer.Option(help="Look name or free-text description")],
    regenerate: Annotated[bool, typer.Option(help="Force LLM regeneration of a described look")] = False,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing .xmp sidecars")] = False,
    wb_lock: Annotated[bool, typer.Option(help="Average correction across the batch")] = False,
    model: Annotated[str, typer.Option(help="LLM model id for described looks")] = "fixture",
) -> None:
    """Decode each RAW, compute correction + look, write an XMP sidecar."""
    conn = connect(db_path())
    try:
        migrate(conn)
        report = run_edit(
            conn, DEFAULT_USER_ID, Path(path), look,
            decoder=_default_decoder(), look_adapter=_default_look_adapter(),
            model=model, knobs=load_correction_knobs(),
            regenerate=regenerate, overwrite=overwrite, wb_lock=wb_lock,
        )
        typer.echo(
            f"look={report.look} processed={report.processed} written={report.written} "
            f"skipped_existing={report.skipped_existing} failed={report.failed}"
        )
    finally:
        conn.close()
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/editing/test_cli_edit.py -v`
Expected: 1 passed.

- [ ] **Step 5: Full suite + lint + import-linter**

Run: `uv run pytest -q`
Expected: all pass.
Run: `uv run ruff check src tests`
Expected: clean.
Run: `uv run lint-imports`
Expected: contracts kept (editing does not import forbidden modules; the look adapter is legal).

- [ ] **Step 6: Commit**

```bash
git add src/shopsteward/editing/cli.py tests/editing/test_cli_edit.py
git commit -m "feat(editing): add `shopsteward edit run` command"
```

---

## Task 11: PRD amendment + operator setup note

**Files:**
- Modify: `docs/PRD_v2.1.md` (§4 flow, §10 milestones)
- Modify: `CLAUDE.md` (Current focus)
- Create: `docs/setup/lightroom-import.md`

- [ ] **Step 1: Amend the PRD**

In `docs/PRD_v2.1.md`:
- §4: add a subsection noting the pivot — automated Etsy gating (scoring/viability/curation) is removed; mass-mode editing now writes XMP sidecars (correction + look) consumed by Lightroom on import; hero/Etsy shop-building is deferred to a later effort sourced from a manual winners folder.
- §10: mark the former scoring/curation milestones as superseded and insert the RAW auto-edit engine (this effort) as the active mass-mode milestone. Cross-reference `docs/superpowers/specs/2026-08-09-raw-auto-edit-engine-design.md`.

- [ ] **Step 2: Update CLAUDE.md Current focus**

Replace the "Current focus" section's forward plan to reflect: mass-mode RAW auto-edit engine (XMP sidecars) is the active work; Etsy gating removed; shop-building deferred. Keep the "PRD wins on disagreement" rule.

- [ ] **Step 3: Write the operator setup note**

`docs/setup/lightroom-import.md`:

```markdown
# Lightroom import setup for ShopSteward sidecars

ShopSteward writes an `.xmp` sidecar next to each RAW **before** you import to
Lightroom. For Lightroom to honor it:

1. Run `shopsteward edit run <folder> --look <name>` on the folder of RAWs first.
2. In Lightroom's Import dialog, set **Apply During Import → Develop Settings = None**.
   If you apply a develop preset or "Auto Settings" here, it stacks on or
   overrides the sidecar.
3. Import normally. Each photo shows the correction + look already applied.

If the RAWs are already in your catalog, Lightroom will not auto-read a new
sidecar — select them and use **Metadata → Read Metadata from File**. Running
ShopSteward before import avoids this.
```

- [ ] **Step 4: Commit**

```bash
git add docs/PRD_v2.1.md CLAUDE.md docs/setup/lightroom-import.md
git commit -m "docs: amend PRD for mass-mode auto-edit pivot + LR import setup"
```

---

## Task 12: Rip out abandoned Etsy gating (SEPARATE PR)

> Do this in its own PR after the engine lands. Scoring/curation is entangled
> with the listings pipeline; treat any surprise coupling as a stop-and-report,
> not a force-through.

**Files:**
- Delete: `src/shopsteward/pipeline/scorers/` and any hero-gate/viability/curation orchestration that consumes it
- Keep: `pipeline/listings/`, `adapters/pod`, `adapters/copy`, `adapters/etsy`, `adapters/meta`, `mockups/` (parked for the later winners-folder effort)
- Modify: `pyproject.toml` import-linter contracts if a deleted module was referenced

- [ ] **Step 1: Map the blast radius**

Run: `uv run python -c "import ast" ` (placeholder — use grep):
Run: `git grep -n "scorers\|viability\|curation\|Gate 1\|awaiting_scoring" src`
Record every referencing site. If `pipeline/listings/` imports scorer outputs at runtime (not just types), STOP and report — the rip-out needs a design decision, not a mechanical delete.

- [ ] **Step 2: Remove the gating code**

Delete the scorer/curation modules identified in Step 1 that have no surviving consumer. Leave listing/mockup/POD code intact.

- [ ] **Step 3: Verify nothing that survives is broken**

Run: `uv run pytest -q`
Expected: pass (delete or update tests that only covered removed gating).
Run: `uv run lint-imports`
Expected: contracts kept.
Run: `uv run ruff check src tests`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: remove abandoned automated Etsy gating (scoring/viability/curation)"
```

---

## Self-review (completed against the spec)

- **Spec coverage:** sidecar-first delivery (Task 5 + Task 11 setup note); conservative correction — as-shot WB + capped, default-off cast nudge (Task 4 + Task 5); explicit correction/look field ownership (Task 5); `--wb-lock` batch consistency (Task 4 `average_corrections` + Task 9); LLM look with own `openrouter.py` (Task 6); RAW-only ingestion (Task 8); described-look normalized-key resolution + `--regenerate` (Task 7); calibration knobs in config (Task 1); rawpy + PRD approvals (Task 1, Task 11); rip-out (Task 12); seed looks for the LLM-quality A/B baseline (Task 1). Open-question items (shop-building un-defer trigger; LLM look quality gate) are tracked in the spec, not implemented here — correct, they are decisions/validation, not code.
- **Placeholder scan:** none — every code step has complete code; Task 12 Step 1 is intentionally investigative with a concrete grep and a stop condition.
- **Type consistency:** `CorrectionSettings`, `LookProfile`, `LookResult`, `EditReport`, `DecodedImage`, `analyze_raw`, `average_corrections`, `compose`, `write_sidecar`, `sidecar_path`, `looks.resolve_look`, `run_edit` names/signatures are consistent across tasks 2–10. `--wb-lock` maps to `wb_lock` param throughout.
