"""Shared builders for compositor/intents/selection tests -- not a
conftest.py fixture module, plain importable helpers."""

import numpy as np

from shopsteward.mockups.models import MockupConfig, StagingTemplate, TemplateRegion


def make_cfg(**render_overrides) -> MockupConfig:
    """Builds a MockupConfig from the shipped defaults shape, with shallow
    overrides applied to the render.* block. Pass light_match_enabled=... as
    a shortcut for the nested render.light_match.enabled toggle."""
    light_match_enabled = render_overrides.pop("light_match_enabled", None)
    base = {
        "schema": "shopsteward.mockups/1",
        "intents": {
            "single": {"enabled": True, "count": 2},
            "gallery_wall": {"enabled": True, "count": 1},
            "framed_poster": {"enabled": True, "count": 1},
            "canvas_edge": {"enabled": True, "count": 1},
            "acrylic": {"enabled": True, "count": 1},
            "digital_whatyougot": {"enabled": True, "count": 1},
        },
        "render": {
            "output_long_edge_px": 2400,
            "jpeg_quality": 90,
            "mat_fraction": 0.06,
            "mat_color": [246, 246, 244],
            "frame_width_inches": 0.75,
            "frame_color": [24, 22, 20],
            "canvas_wrap_depth_inches": 1.25,
            "shadow": {"offset_frac": 0.006, "blur_frac": 0.010, "opacity": 0.35, "angle_deg": 115},
            "light_match": {
                "enabled": True,
                "gain_min": 0.65,
                "gain_max": 1.15,
                "wb_min": 0.90,
                "wb_max": 1.10,
            },
        },
        "products": {
            "default_print_widths_inches": {"landscape": 24, "portrait": 18, "square": 20}
        },
        "whatyougot": {
            "sizes": ["4x5", "5x7", "8x10", "11x14", "16x20"],
            "formats": ["JPEG 300 DPI", "sRGB"],
            "headline": "Instant Digital Download",
        },
        "listing_copy": {
            "ai_disclosure_line": (
                "Room scenes are AI-generated staging mockups. The photograph itself is the "
                "artist's original work and is never AI-generated or AI-edited."
            )
        },
    }
    base["render"].update(render_overrides)
    if light_match_enabled is not None:
        base["render"]["light_match"]["enabled"] = light_match_enabled
    return MockupConfig.model_validate(base)


def checkerboard(size: int = 400, cell: int = 50) -> np.ndarray:
    """An 8x8-cell checkerboard (default 400px/50px cell), TL cell white."""
    board = np.zeros((size, size, 3), dtype=np.uint8)
    for i in range(0, size, cell):
        for j in range(0, size, cell):
            if ((i // cell) + (j // cell)) % 2 == 0:
                board[i : i + cell, j : j + cell] = 255
    return board


def flat_template(size: int = 1000, color: tuple[int, int, int] = (128, 128, 128)) -> np.ndarray:
    return flat_template_rect(size, size, color)


def flat_template_rect(
    width: int, height: int, color: tuple[int, int, int] = (120, 120, 120)
) -> np.ndarray:
    img = np.empty((height, width, 3), dtype=np.uint8)
    img[:, :] = color
    return img


def random_photo(width: int = 1600, height: int = 1200, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def solid_photo(
    width: int = 1600, height: int = 1200, color: tuple[int, int, int] = (210, 40, 40)
) -> np.ndarray:
    """A flat, saturated-color photo -- unlike random per-pixel noise (which
    regresses toward mid-gray under any resampling/blur), a solid color
    reliably reads as a large, deterministic delta against any gray
    background, which is what centroid-shift pixel-sanity checks need."""
    img = np.empty((height, width, 3), dtype=np.uint8)
    img[:, :] = color
    return img


def make_region(
    quad: list[list[float]], width_inches: float, kind: str = "wall_print"
) -> TemplateRegion:
    return TemplateRegion(kind=kind, quad=quad, region_width_inches=width_inches)


def make_template(
    template_id: str,
    regions: list[TemplateRegion],
    orientation: str = "landscape",
    room_type: str = "living_room",
    style: str = "modern",
    lighting: str = "warm_daylight",
    tags: list[str] | None = None,
) -> StagingTemplate:
    return StagingTemplate(
        schema="shopsteward.stagingtemplate/1",
        template_id=template_id,
        room_type=room_type,
        style=style,
        lighting=lighting,
        orientation=orientation,
        regions=regions,
        tags=tags or [],
    )
