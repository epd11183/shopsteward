"""Deterministic taste guard for LLM-generated looks. Hand-authored seed looks
are trusted and skip this; a freshly generated look that trips a bounded
aggregate check is rejected (caller retries once, then falls back to a seed).
Thresholds are calibration knobs. ponytail: heuristic aggregate caps, tune the
knobs if they reject good looks or pass garish ones."""

from pydantic import BaseModel

from shopsteward.adapters.look.interface import LookProfile


class LookVerdict(BaseModel):
    ok: bool
    reason: str | None = None


def _tone_steepness(points: list[list[int]]) -> int:
    return max((abs(int(y) - int(x)) for x, y in points), default=0) if points else 0


def sanitize_look(profile: LookProfile, knobs: dict) -> LookVerdict:
    sat_load = abs(profile.vibrance) + abs(profile.saturation)
    sat_load += sum(abs(v) for k, v in profile.hsl.items() if "Saturation" in k)
    sat_load += sum(abs(v) for k, v in profile.split_toning.items() if k.endswith("Saturation"))
    if sat_load > knobs.get("max_saturation_load", 220):
        return LookVerdict(ok=False, reason=f"saturation load {sat_load} over cap")

    contrast_tone = abs(profile.contrast) + _tone_steepness(profile.tone_curve)
    if contrast_tone > knobs.get("max_contrast_tone", 140):
        return LookVerdict(ok=False, reason=f"contrast+tone {contrast_tone} over cap")

    presence = (
        abs(profile.clarity)
        + abs(profile.dehaze)
        + abs(profile.texture)
        + abs(profile.highlights)
        + abs(profile.whites)
        + abs(profile.blacks)
    )
    if presence > knobs.get("max_presence_load", 200):
        return LookVerdict(ok=False, reason=f"presence load {presence} over cap")

    cap = knobs.get("max_split_saturation", 60)
    for k, v in profile.split_toning.items():
        if k.endswith("Saturation") and v > cap:
            return LookVerdict(ok=False, reason=f"split-tone {k}={v} over cap")

    return LookVerdict(ok=True, reason=None)
