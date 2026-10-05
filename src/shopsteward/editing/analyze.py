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
    shadows = _shadow(luma, knobs)
    # Lifting Shadows2012 without deepening Blacks2012 reads as flat/hazy/milky
    # (visually verified 2026-09-16); a shadow push earns a proportional black
    # deepening to keep contrast/punch. Vibrance gets a matching nudge since a
    # shadow lift also visually desaturates the recovered region.
    shadow_black_bonus = int(round(shadows * float(knobs.get("shadow_black_ratio", 0.35))))
    black_point = max(-80, _black_point(luma, knobs) - shadow_black_bonus)
    vibrance_boost = int(round(shadows * float(knobs.get("shadow_vibrance_ratio", 0.25))))

    temperature = tint = None
    if knobs.get("auto_white_balance"):
        temperature, tint = estimate_wb(decoded, knobs)

    luminance_nr, color_nr = _denoise(decoded.exif, knobs)

    return CorrectionSettings(
        exposure=exposure,
        highlight_recovery=_highlight_recovery(luma, knobs),
        black_point=black_point,
        shadows=shadows,
        vibrance_boost=vibrance_boost,
        temperature=temperature,
        tint=tint,
        lens_profile=bool(knobs.get("lens_profile_corrections", False)),
        remove_ca=bool(knobs.get("remove_chromatic_aberration", False)),
        luminance_nr=luminance_nr,
        color_nr=color_nr,
        luminance_detail=int(knobs.get("nr_luminance_detail", 50)),
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


def _shadow(luma: np.ndarray, knobs: dict) -> int:
    """Global Shadows2012 push, scaled to how deficient the dark quartile is.
    ponytail: previously a local luminance-range-masked exposure boost
    (MaskGroupBasedCorrections); dropped because that mask was never confirmed
    to render in real Lightroom (visually verified against real underexposed
    frames, 2026-09-16) and Shadows2012 is a plain, universally-supported
    PV2012 slider that does the same job."""
    trigger = float(knobs["shadow_trigger_luma"])
    push_max = float(knobs["shadow_lift_max"])
    # Mean luma of the darkest quartile as the shadow proxy.
    dark = luma[luma <= np.quantile(luma, 0.25)]
    dark_mean = float(dark.mean()) if dark.size else float(luma.mean())
    if dark_mean >= trigger:
        return 0
    # Deeper shadows -> more push, scaled to the cap. A <1 exponent front-loads
    # the curve so moderately-deficient frames still get a meaningful push
    # instead of only the most extreme frames reaching useful strength
    # (visually verified against real underexposed frames, 2026-09-16: a
    # linear deficit left subjects still visibly dark).
    deficit = (trigger - dark_mean) / trigger
    curved = deficit**0.6
    return int(round(min(push_max, push_max * curved)))


def _denoise(exif: dict, knobs: dict) -> tuple[int, int]:
    """ISO -> noise-reduction curve. Below the floor ISO, no luminance NR is
    needed and color NR sits at its base; above the ceiling, luminance NR
    saturates at its max on a log2 (stop-linear) ramp between the two."""
    iso = exif.get("ISOSpeedRatings")
    floor = float(knobs["nr_iso_floor"])
    ceiling = float(knobs["nr_iso_ceiling"])
    lum_max = float(knobs["nr_luminance_max"])
    color_base = float(knobs["nr_color_base"])
    color_max = float(knobs["nr_color_max"])
    if not iso or iso <= 0:
        return 0, int(round(color_base))
    t = math.log2(iso / floor) / math.log2(ceiling / floor)
    t = max(0.0, min(1.0, t))
    luminance_nr = int(round(lum_max * t))
    color_nr = int(round(color_base + (color_max - color_base) * t))
    return luminance_nr, color_nr


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
        shadows=int(round(sum(c.shadows for c in items) / n)),
        vibrance_boost=int(round(sum(c.vibrance_boost for c in items) / n)),
        temperature=int(round(sum(c.temperature for c in items) / n)) if all_wb else None,
        tint=int(round(sum(c.tint for c in items) / n)) if all_wb else None,
        lens_profile=items[0].lens_profile,
        remove_ca=items[0].remove_ca,
        luminance_nr=int(round(sum(c.luminance_nr for c in items) / n)),
        color_nr=int(round(sum(c.color_nr for c in items) / n)),
        luminance_detail=items[0].luminance_detail,
    )
