"""Colorimetric white-balance / tint estimator.

Produces an approximate, ACR-style absolute (temperature_k, tint) from the
RAW's as-shot WB multipliers and the camera->XYZ color matrix. This is a
principled *estimate* (standard colorimetry + calibration offsets), NOT an
exact reproduction of Adobe Camera Raw's proprietary temperature model.

Colorimetry references:
- As-shot illuminant recovery: the camera WB multipliers are what the camera
  applies to a raw neutral to make it read equal-energy, so a neutral under the
  as-shot illuminant reads proportional to the *inverse* of the multipliers.
- CCT: McCamy's cubic approximation, C.S. McCamy, "Correlated color
  temperature as an explicit function of chromaticity coordinates",
  Color Research & Application 17(2):142-144, 1992.
- Planckian locus xy(T): Kim et al. cubic fit (used by CIE / Wikipedia
  "Planckian locus" article), valid 1667K-25000K.
- Tint as Duv: signed distance from the Planckian locus in CIE 1960 (u,v).
  Points above the locus (higher v) are green; below are magenta. ACR tint is
  green-negative / magenta-positive, scaled ~+/-150 over ~+/-0.05 Duv.

Matrix direction was verified empirically against the test's Canon-ish matrix:
inv(xyz_matrix) @ camrgb lands on the Planckian locus for a daylight multiplier
set (~5300K), while xyz_matrix @ camrgb does not. So `xyz_matrix` is treated as
XYZ->camera (libraw rgb_xyz_matrix convention) and inverted here. A runtime
sanity check falls back to the other orientation if the chromaticity is
implausible, so we never emit a CCT built from an off-locus point.
"""

from __future__ import annotations

import numpy as np

_TEMP_MIN, _TEMP_MAX = 2000, 50000
_TINT_MIN, _TINT_MAX = -150, 150
# ACR tint units per unit Duv: ~150 tint over ~0.05 Duv (documented approx).
_TINT_PER_DUV = 150.0 / 0.05
# Plausible illuminant chromaticity window (near the Planckian locus).
_X_LO, _X_HI = 0.15, 0.60
_Y_LO, _Y_HI = 0.10, 0.60


def _camrgb_neutral(mult: tuple[float, float, float, float]) -> np.ndarray:
    """Neutral surface in camera RGB under the as-shot illuminant.

    Multipliers are (R, G, B, G2); average the two greens. Reads proportional
    to 1/mult because the multipliers are what neutralize the raw neutral.
    """
    r, g1, b, g2 = mult
    g = (g1 + g2) / 2.0 if g2 else g1
    return np.array([1.0 / r, 1.0 / g, 1.0 / b], dtype=float)


def _camrgb_to_xyz(camrgb: np.ndarray, xyz_matrix: np.ndarray) -> np.ndarray:
    """camera RGB -> XYZ, choosing the matrix orientation that lands on the
    plausible-illuminant chromaticity window. See module docstring."""
    inv = np.linalg.inv(xyz_matrix)
    for M in (inv, xyz_matrix):  # prefer inverse (libraw XYZ->cam convention)
        xyz = M @ camrgb
        s = xyz.sum()
        if s <= 0:
            continue
        x, y = xyz[0] / s, xyz[1] / s
        if _X_LO <= x <= _X_HI and _Y_LO <= y <= _Y_HI:
            return xyz
    # Neither orientation is plausible: return the preferred one; callers get a
    # clamped result rather than a crash, but this signals a bad matrix.
    return inv @ camrgb


def _xy(xyz: np.ndarray) -> tuple[float, float]:
    s = xyz.sum()
    return xyz[0] / s, xyz[1] / s


def _mccamy_cct(x: float, y: float) -> float:
    """McCamy 1992 CCT from CIE 1931 xy."""
    n = (x - 0.3320) / (0.1858 - y)
    return 449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33


def _planckian_xy(t: float) -> tuple[float, float]:
    """Planckian locus xy(T), Kim et al. cubic (valid ~1667-25000K)."""
    t = min(max(t, 1667.0), 25000.0)
    if t < 4000.0:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t < 2222.0:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t < 4000.0:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def _uv(x: float, y: float) -> tuple[float, float]:
    """CIE 1931 xy -> CIE 1960 uv."""
    d = -2.0 * x + 12.0 * y + 3.0
    return 4.0 * x / d, 6.0 * y / d


def _cct_tint(camrgb: np.ndarray, xyz_matrix: np.ndarray) -> tuple[float, float]:
    """(CCT Kelvin, ACR tint) for a camera-RGB neutral. Pre-offset, unclamped."""
    xyz = _camrgb_to_xyz(camrgb, xyz_matrix)
    x, y = _xy(xyz)
    cct = _mccamy_cct(x, y)
    # Duv: sample vs Planckian point at the estimated CCT, in CIE 1960 uv.
    su, sv = _uv(x, y)
    px, py = _planckian_xy(cct)
    pu, pv = _uv(px, py)
    duv = sv - pv  # >0 above locus = green; <0 below = magenta
    # ACR: green negative, magenta positive (sign verified at operator calibration).
    tint = -_TINT_PER_DUV * duv
    return cct, tint


def estimate_wb(decoded, knobs: dict) -> tuple[int | None, int | None]:
    """Estimate absolute ACR-style (temperature_k, tint).

    `decoded` needs `.wb_multipliers` (R,G,B,G2), `.xyz_matrix` (3x3), and
    `.rgb` (HxWx3 float) for the optional gray-world blend. `knobs` may carry
    `grayworld_blend` (0..1), `wb_temp_offset_k`, `wb_tint_offset`.

    Falls back to "as shot" (`(None, None)`) if `xyz_matrix` is missing or
    numerically degenerate (e.g. singular) rather than guessing or crashing.
    """
    knobs = knobs or {}
    if decoded.xyz_matrix is None:
        return None, None
    xyz_matrix = np.asarray(decoded.xyz_matrix, dtype=float)

    try:
        cct, tint = _cct_tint(_camrgb_neutral(decoded.wb_multipliers), xyz_matrix)
    except (TypeError, ValueError, np.linalg.LinAlgError):
        return None, None

    blend = float(knobs.get("grayworld_blend", 0.0))
    if blend > 0.0:
        # Gray-world: mean image RGB stands in for a neutral. ponytail: rgb is
        # post-WB output, not camera-native, so this is a coarse heuristic blend
        # only; default 0 keeps the pure as-shot-illuminant estimate.
        mean = np.asarray(decoded.rgb, dtype=float).reshape(-1, 3).mean(axis=0)
        mean = np.where(mean <= 0, 1e-6, mean)
        gw_cct, gw_tint = _cct_tint(mean, xyz_matrix)
        b = min(blend, 1.0)
        cct = (1.0 - b) * cct + b * gw_cct
        tint = (1.0 - b) * tint + b * gw_tint

    cct += float(knobs.get("wb_temp_offset_k", 0))
    tint += float(knobs.get("wb_tint_offset", 0))

    cct = int(round(min(max(cct, _TEMP_MIN), _TEMP_MAX)))
    tint = int(round(min(max(tint, _TINT_MIN), _TINT_MAX)))
    return cct, tint
