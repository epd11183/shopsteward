"""Pure objective-correction engine. Input: a decoded RAW + calibration knobs.
Output: CorrectionSettings. No I/O, no rawpy, no XMP — deterministic and unit-
testable on synthetic arrays."""

import math

import numpy as np

from shopsteward.editing.models import CorrectionSettings
from shopsteward.editing.rawdecode import DecodedImage
from shopsteward.editing.whitebalance import estimate_wb

# Rec. 709 luma weights.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ _LUMA


def analyze_raw(decoded: DecodedImage, knobs: dict) -> CorrectionSettings:
    rgb = np.clip(decoded.rgb.astype(np.float32), 0.0, 1.0)
    luma = _luma(rgb)

    exposure = _exposure(luma, knobs)
    shadow_lift, lo, hi = _shadow(luma, knobs)

    temperature = tint = None
    if knobs.get("auto_white_balance"):
        temperature, tint = estimate_wb(decoded, knobs)

    return CorrectionSettings(
        exposure=exposure,
        highlight_recovery=_highlight_recovery(luma, knobs),
        black_point=_black_point(luma, knobs),
        shadow_lift=shadow_lift,
        shadow_range_low=lo,
        shadow_range_high=hi,
        temperature=temperature,
        tint=tint,
        lens_profile=bool(knobs.get("lens_profile_corrections", False)),
        remove_ca=bool(knobs.get("remove_chromatic_aberration", False)),
    )


def _exposure(luma: np.ndarray, knobs: dict) -> float:
    target = float(knobs["exposure_target_luma"])
    cap = float(knobs["exposure_max_stops"])
    ceiling = float(knobs.get("exposure_highlight_ceiling", 0.92))
    median = float(np.median(luma))
    stops = cap if median <= 1e-4 else math.log2(target / median)
    if stops > 0:
        # Protect highlights: never brighten past the point the bright end (p99)
        # would clip. A blown sky caps the positive push at ~0; the subject is
        # recovered by the local shadow lift + the look's Highlights slider.
        p_high = float(np.quantile(luma, 0.99))
        if p_high > 1e-4:
            max_up = math.log2(ceiling / p_high)  # stops until p99 reaches the ceiling
            stops = min(stops, max(0.0, max_up))
    # Global operator calibration for the rawpy-vs-Lightroom render offset:
    # a uniform stop shift applied to every frame. Negative = darker overall.
    stops += float(knobs.get("exposure_bias", 0.0))
    return round(max(-cap, min(cap, stops)), 2)


def _highlight_recovery(luma: np.ndarray, knobs: dict) -> int:
    """Adaptive Highlights2012: pull the bright end down in proportion to how
    much of the frame is near clipping. A blown sky gets strong recovery; a frame
    with no hot highlights gets 0. Returns a value in [-max, 0]."""
    thresh = float(knobs.get("highlight_clip_threshold", 0.90))
    max_recovery = int(knobs.get("highlight_recovery_max", 70))
    saturate = float(knobs.get("highlight_recovery_saturate", 0.15))
    frac = float((luma >= thresh).mean())
    strength = min(1.0, frac / saturate) if saturate > 0 else 0.0
    return -int(round(max_recovery * strength))


def _black_point(luma: np.ndarray, knobs: dict) -> int:
    """Adaptive Blacks2012: deepen the black point only when the darkest pixels
    are lifted/hazy (restores contrast on flat frames); leave already-crushed
    frames alone. Returns a value in [-max, 0]."""
    target = float(knobs.get("black_point_target", 0.02))
    max_deepen = int(knobs.get("black_point_max", 25))
    saturate = float(knobs.get("black_point_saturate", 0.15))
    p_low = float(np.quantile(luma, 0.01))
    if p_low <= target:
        return 0  # already has true blacks
    strength = min(1.0, (p_low - target) / max(1e-4, saturate - target))
    return -int(round(max_deepen * strength))


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


def average_corrections(items: list[CorrectionSettings]) -> CorrectionSettings:
    """Batch/sequence lock: mean of continuous corrections applied to all frames."""
    # Consistency over per-frame optimum: a single dark frame's lift is diluted across the batch.
    n = len(items)
    if n == 0:
        return CorrectionSettings()
    all_wb = all(c.temperature is not None and c.tint is not None for c in items)
    return CorrectionSettings(
        exposure=round(sum(c.exposure for c in items) / n, 2),
        highlight_recovery=int(round(sum(c.highlight_recovery for c in items) / n)),
        black_point=int(round(sum(c.black_point for c in items) / n)),
        shadow_lift=round(sum(c.shadow_lift for c in items) / n, 2),
        shadow_range_low=items[0].shadow_range_low,
        shadow_range_high=items[0].shadow_range_high,
        temperature=int(round(sum(c.temperature for c in items) / n)) if all_wb else None,
        tint=int(round(sum(c.tint for c in items) / n)) if all_wb else None,
        lens_profile=items[0].lens_profile,
        remove_ca=items[0].remove_ca,
    )
