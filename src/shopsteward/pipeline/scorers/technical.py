"""OpenCV/Pillow technical scorer: Laplacian sharpness, exposure histogram,
noise estimate; normalization curves come from the tuning profile."""

import cv2
import numpy as np

from shopsteward.pipeline.scorers import ScoreContext, ScorerResult, register

_RESOLUTION_GUARD_CAP = 40.0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


class TechnicalScorer:
    name = "technical"

    def score(self, ctx: ScoreContext) -> ScorerResult | None:
        cfg = ctx.profile.scoring.technical

        gray = cv2.imread(ctx.jpeg_path, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise ValueError(f"unreadable image: {ctx.jpeg_path}")

        height, width = gray.shape
        long_edge = max(height, width)

        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        variance = float(laplacian.var())
        floor, ceiling = cfg["laplacian_floor"], cfg["laplacian_ceiling"]
        sharpness_score = _clamp(100 * (variance - floor) / (ceiling - floor))

        total_px = gray.size
        shadow_clip_pct = 100 * float(np.count_nonzero(gray <= 5)) / total_px
        highlight_clip_pct = 100 * float(np.count_nonzero(gray >= 250)) / total_px
        shadow_penalty = max(0.0, shadow_clip_pct - cfg["clip_shadow_pct_max"]) * 10
        highlight_penalty = max(0.0, highlight_clip_pct - cfg["clip_highlight_pct_max"]) * 10
        exposure_score = _clamp(100 - shadow_penalty - highlight_penalty)

        # Quick MAD-based noise-sigma estimate (a well-known shortcut): the
        # median absolute Laplacian response scales with sensor noise sigma
        # under the 0.6745 Gaussian-consistency constant.
        sigma = float(np.median(np.abs(laplacian))) / 0.6745
        noise_score = _clamp(100 * (1 - sigma / cfg["noise_sigma_ceiling"]))

        overall = (sharpness_score + exposure_score + noise_score) / 3

        detail = {
            "sharpness_score": round(sharpness_score, 1),
            "exposure_score": round(exposure_score, 1),
            "noise_score": round(noise_score, 1),
            "laplacian_variance": round(variance, 1),
            "shadow_clip_pct": round(shadow_clip_pct, 2),
            "highlight_clip_pct": round(highlight_clip_pct, 2),
            "noise_sigma": round(sigma, 2),
            "long_edge_px": long_edge,
        }

        min_long_edge = cfg["min_long_edge_px"]
        if long_edge < min_long_edge:
            overall = min(overall, _RESOLUTION_GUARD_CAP)
            detail["resolution_guard_applied"] = True
            detail["min_long_edge_px"] = min_long_edge

        return ScorerResult(score=round(_clamp(overall), 1), detail=detail)


register(TechnicalScorer())
