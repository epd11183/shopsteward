"""RAW decode seam. Real decode uses rawpy (libraw); tests use FakeRawDecoder
so no RAW files are committed (CLAUDE.md hard guardrail)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from shopsteward.editing.exifread import read_cr3_exif, read_jpeg_exif, sibling_jpeg

# Downscale target for analysis; WB/exposure/shadow stats are stable well below
# full resolution and this keeps decode fast. ponytail: fixed longest-edge box,
# revisit only if estimates prove noisy.
_ANALYSIS_LONG_EDGE = 1024


@dataclass
class DecodedImage:
    rgb: np.ndarray  # HxWx3, float32 in [0, 1]
    wb_multipliers: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 0.0)
    xyz_matrix: np.ndarray | None = None  # 3x3 libraw rgb_xyz_matrix (XYZ->camera)
    exif: dict = field(default_factory=dict)
    camera_mean: tuple[float, float, float] | None = (
        None  # camera-native linear RGB, for gray-world WB
    )


class RawDecoder(Protocol):
    def decode(self, raw_path: str) -> DecodedImage: ...


class RawpyDecoder:
    def decode(self, raw_path: str) -> DecodedImage:
        import rawpy  # local import: keeps the native dep out of the import graph for fakes

        with rawpy.imread(raw_path) as raw:
            wb = tuple(float(x) for x in raw.camera_whitebalance[:4])
            # First 3 rows of the libraw rgb_xyz_matrix; estimator orients it.
            xyz_matrix = np.array(raw.rgb_xyz_matrix, dtype=np.float64)[:3]
            camera_mean = _camera_mean(raw)
            rgb16 = raw.postprocess(output_bps=16, no_auto_bright=True, use_camera_wb=True)
        rgb = rgb16.astype(np.float32) / 65535.0
        rgb = _downscale(rgb, _ANALYSIS_LONG_EDGE)
        jpeg_path = sibling_jpeg(Path(raw_path))
        exif = read_jpeg_exif(jpeg_path) if jpeg_path is not None else read_cr3_exif(raw_path)
        return DecodedImage(
            rgb=rgb, wb_multipliers=wb, xyz_matrix=xyz_matrix, exif=exif, camera_mean=camera_mean
        )


def _camera_mean(raw) -> tuple[float, float, float] | None:
    """Mid-tone mean of the black-subtracted Bayer planes (no WB, no demosaic).
    Clipped and darkest pixels are excluded so blown highlights and noise floor
    don't skew it. Non-2x2 CFAs (X-Trans) return None -> gray-world is skipped."""
    cols = raw.raw_colors_visible
    if raw.raw_pattern is None or raw.raw_pattern.shape != (2, 2):
        return None
    img = raw.raw_image_visible.astype(np.float32)
    h, w = (img.shape[0] // 2) * 2, (img.shape[1] // 2) * 2
    black = raw.black_level_per_channel
    planes: dict[int, list[np.ndarray]] = {0: [], 1: [], 2: []}
    for i in (0, 1):
        for j in (0, 1):
            c = int(cols[i, j])
            planes[1 if c == 3 else c].append(img[i:h:2, j:w:2] - black[c])
    if not all(planes.values()):
        return None
    r, g, b = (np.mean(planes[k], axis=0) for k in (0, 1, 2))
    clip = 0.98 * (raw.white_level - max(black))
    y = (r + g + b) / 3.0
    m = (y > np.percentile(y, 20)) & (np.maximum(np.maximum(r, g), b) < clip)
    if m.sum() < 100:
        return None
    mean = (float(r[m].mean()), float(g[m].mean()), float(b[m].mean()))
    return mean if min(mean) > 0 else None


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
