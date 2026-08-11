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
    xyz_matrix: np.ndarray | None = None  # 3x3 libraw rgb_xyz_matrix (XYZ->camera)
    exif: dict = field(default_factory=dict)


class RawDecoder(Protocol):
    def decode(self, raw_path: str) -> DecodedImage: ...


class RawpyDecoder:
    def decode(self, raw_path: str) -> DecodedImage:
        import rawpy  # local import: keeps the native dep out of the import graph for fakes

        with rawpy.imread(raw_path) as raw:
            wb = tuple(float(x) for x in raw.camera_whitebalance[:4])
            # First 3 rows of the libraw rgb_xyz_matrix; estimator orients it.
            xyz_matrix = np.array(raw.rgb_xyz_matrix, dtype=np.float64)[:3]
            rgb16 = raw.postprocess(output_bps=16, no_auto_bright=True, use_camera_wb=True)
        rgb = rgb16.astype(np.float32) / 65535.0
        rgb = _downscale(rgb, _ANALYSIS_LONG_EDGE)
        return DecodedImage(rgb=rgb, wb_multipliers=wb, xyz_matrix=xyz_matrix, exif={})


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
