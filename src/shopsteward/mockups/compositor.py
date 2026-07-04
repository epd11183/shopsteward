"""Pure numpy/OpenCV compositor functions for staging print mockups.

No DB, no events, no network -- everything here is a deterministic function
of its arguments. Callers (jobs.py) own I/O and orchestration.

Color convention: every array is RGB (never BGR), uint8, shape (H, W, 3) for
opaque images or (H, W, 4) for RGBA layers with straight (non-premultiplied)
alpha. Arrays enter and leave this module via PIL (``Image.open`` ->
``np.asarray`` -> ``Image.fromarray``); OpenCV functions are called on those
RGB arrays directly, which is safe here because nothing in this module
depends on channel semantics beyond an explicit luma formula.
"""

import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms

from shopsteward.mockups.models import MockupConfig, TemplateRegion, _ShadowConfig

_ORIENTATION_SQUARE_TOLERANCE = 0.05


def photo_orientation(width: float, height: float) -> str:
    """landscape / portrait / square (5% aspect tolerance) classification."""
    aspect = width / height
    if abs(aspect - 1.0) <= _ORIENTATION_SQUARE_TOLERANCE:
        return "square"
    return "landscape" if aspect > 1.0 else "portrait"


def prep_master(path: Path, max_long_edge: int) -> np.ndarray:
    """Opens the photo master, converts to sRGB 8-bit RGB, downscales if the
    long edge exceeds max_long_edge. 16-bit modes or images carrying a
    non-sRGB ICC profile are colour-managed via ImageCms; if the embedded
    profile can't be parsed we fall back to a plain RGB convert rather than
    raising (never a bare except -- only the specific ImageCms/IO failures)."""
    with Image.open(path) as img:
        img.load()
        icc_bytes = img.info.get("icc_profile")
        needs_base_convert = img.mode not in ("RGB", "RGBA")

        rgb = None
        if icc_bytes:
            try:
                src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                srgb_profile = ImageCms.createProfile("sRGB")
                base = img.convert("RGB") if needs_base_convert else img
                rgb = ImageCms.profileToProfile(base, src_profile, srgb_profile, outputMode="RGB")
            except (ImageCms.PyCMSError, OSError, ValueError):
                rgb = None

        if rgb is None:
            rgb = img.convert("RGB")

        long_edge = max(rgb.size)
        if long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            new_size = (max(1, round(rgb.size[0] * scale)), max(1, round(rgb.size[1] * scale)))
            rgb = rgb.resize(new_size, Image.LANCZOS)

        return np.asarray(rgb.convert("RGB"), dtype=np.uint8)


def region_ppi(region: TemplateRegion) -> float:
    """Pixels per inch implied by the region's top edge (TL -> TR)."""
    tl = np.array(region.quad[0], dtype=float)
    tr = np.array(region.quad[1], dtype=float)
    top_edge_px = float(np.linalg.norm(tr - tl))
    return top_edge_px / region.region_width_inches


def _region_height_inches(region: TemplateRegion, ppi: float) -> float:
    quad = np.array(region.quad, dtype=float)
    tl, tr, br, bl = quad
    left_px = np.linalg.norm(bl - tl)
    right_px = np.linalg.norm(br - tr)
    return float((left_px + right_px) / 2 / ppi)


def print_rect_inches(
    photo_w: float, photo_h: float, region: TemplateRegion, cfg: MockupConfig
) -> tuple[float, float]:
    """Print size in inches: default width by photo orientation, height from
    photo aspect, then scaled down (never up, never cropped) to fit within
    the region's mat-adjusted bounds."""
    orientation = photo_orientation(photo_w, photo_h)
    w_in = cfg.products.default_print_widths_inches[orientation]
    h_in = w_in * (photo_h / photo_w)

    mat_frac = cfg.render.mat_fraction
    ppi = region_ppi(region)
    region_height_in = _region_height_inches(region, ppi)

    max_w_in = region.region_width_inches * (1 - 2 * mat_frac)
    max_h_in = region_height_in * (1 - 2 * mat_frac)

    scale = min(1.0, max_w_in / w_in, max_h_in / h_in)
    if scale < 1.0:
        w_in *= scale
        h_in *= scale
    return w_in, h_in


def target_quad(region: TemplateRegion, w_in: float, h_in: float) -> np.ndarray:
    """print+mat rect of size (w_in, h_in), centered in the region, mapped
    into image space via bilinear interpolation over the region quad:
    P(u,v) = (1-u)(1-v)TL + u(1-v)TR + uv*BR + (1-u)v*BL."""
    quad = np.array(region.quad, dtype=float)
    tl, tr, br, bl = quad
    ppi = region_ppi(region)
    region_width_in = region.region_width_inches
    region_height_in = _region_height_inches(region, ppi)

    u_margin = (1 - w_in / region_width_in) / 2
    v_margin = (1 - h_in / region_height_in) / 2

    corners_uv = [
        (u_margin, v_margin),
        (1 - u_margin, v_margin),
        (1 - u_margin, 1 - v_margin),
        (u_margin, 1 - v_margin),
    ]

    def bilerp(u: float, v: float) -> np.ndarray:
        return (1 - u) * (1 - v) * tl + u * (1 - v) * tr + u * v * br + (1 - u) * v * bl

    return np.array([bilerp(u, v) for u, v in corners_uv], dtype=np.float32)


def light_stats(
    template: np.ndarray, quad: np.ndarray, cfg: MockupConfig
) -> tuple[float, float, float]:
    """(gain, wb_r, wb_b) sampled from the neighborhood ring around quad:
    dilate(quad fill, 4% of quad width) minus the quad fill itself. Empty
    neighborhood (e.g. quad touching the image edge) -> no-op (1, 1, 1)."""
    h, w = template.shape[:2]
    quad_int = np.round(quad).astype(np.int32)

    quad_mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillConvexPoly(quad_mask, quad_int, 255)

    quad_width_px = float(np.linalg.norm(quad[1] - quad[0]))
    kernel_size = max(1, int(round(0.04 * quad_width_px)))
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    dilated = cv2.dilate(quad_mask, kernel)

    neighborhood_mask = (dilated > 0) & (quad_mask == 0)

    lm = cfg.render.light_match
    if not np.any(neighborhood_mask):
        return 1.0, 1.0, 1.0

    pixels = template[neighborhood_mask].astype(np.float64)
    r_mean, g_mean, b_mean = pixels.mean(axis=0)

    luma = 0.299 * r_mean + 0.587 * g_mean + 0.114 * b_mean
    gain = float(np.clip(luma / 128.0, lm.gain_min, lm.gain_max))
    wb_r = float(np.clip(r_mean / g_mean if g_mean > 0 else 1.0, lm.wb_min, lm.wb_max))
    wb_b = float(np.clip(b_mean / g_mean if g_mean > 0 else 1.0, lm.wb_min, lm.wb_max))
    return gain, wb_r, wb_b


def apply_mat(print_img: np.ndarray, mat_frac: float, mat_color: list[int]) -> np.ndarray:
    """Adds a solid mat_color border sized mat_frac * short-edge-in-px."""
    h, w = print_img.shape[:2]
    channels = print_img.shape[2]
    border = max(0, int(round(mat_frac * min(h, w))))

    color = list(mat_color)
    if channels == 4 and len(color) == 3:
        color = [*color, 255]

    out = np.empty((h + 2 * border, w + 2 * border, channels), dtype=print_img.dtype)
    out[:, :] = color
    out[border : border + h, border : border + w] = print_img
    return out


def draw_frame_border(
    img: np.ndarray, frame_px: float, frame_color: list[int], bevel: bool = True
) -> np.ndarray:
    """Adds a solid frame_color border frame_px wide, with an optional 2px
    lighter bevel highlight along the top and left edges."""
    h, w = img.shape[:2]
    channels = img.shape[2]
    frame_px_i = max(1, int(round(frame_px)))

    color = list(frame_color)
    if channels == 4 and len(color) == 3:
        color = [*color, 255]

    out = np.empty((h + 2 * frame_px_i, w + 2 * frame_px_i, channels), dtype=img.dtype)
    out[:, :] = color
    out[frame_px_i : frame_px_i + h, frame_px_i : frame_px_i + w] = img

    if bevel:
        bevel_px = min(2, frame_px_i)
        highlight = np.clip(np.array(color[:3], dtype=np.int32) + 40, 0, 255).tolist()
        if channels == 4:
            highlight = [*highlight, 255]
        out[0:bevel_px, :] = highlight
        out[:, 0:bevel_px] = highlight
    return out


def draw_shadow(canvas: np.ndarray, quad: np.ndarray, shadow_cfg: _ShadowConfig) -> np.ndarray:
    """Paints a soft drop shadow of quad's silhouette onto canvas: offset
    offset_frac * quad-width along angle_deg, blurred blur_frac * quad-width,
    darkened by opacity. Returns a new array; canvas is not mutated."""
    h, w = canvas.shape[:2]
    quad_w = float(np.linalg.norm(quad[1] - quad[0]))
    angle_rad = np.deg2rad(shadow_cfg.angle_deg)
    offset = shadow_cfg.offset_frac * quad_w
    dx, dy = offset * np.cos(angle_rad), offset * np.sin(angle_rad)

    shadow_quad = (quad + np.array([dx, dy])).astype(np.int32)
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillConvexPoly(mask, shadow_quad, 255)

    blur_px = max(1, int(round(shadow_cfg.blur_frac * quad_w)))
    if blur_px % 2 == 0:
        blur_px += 1
    blurred = cv2.GaussianBlur(mask, (blur_px, blur_px), 0)
    alpha = (blurred.astype(np.float64) / 255.0) * shadow_cfg.opacity

    out = canvas.astype(np.float64) * (1 - alpha[..., None])
    return np.clip(out, 0, 255).astype(np.uint8)


def _apply_light_match(img_rgb: np.ndarray, gain: float, wb_r: float, wb_b: float) -> np.ndarray:
    lit = img_rgb.astype(np.float64)
    lit[..., 0] *= gain * wb_r
    lit[..., 1] *= gain
    lit[..., 2] *= gain * wb_b
    return np.clip(lit, 0, 255).astype(np.uint8)


def _warp_rgba_onto(base_rgb: np.ndarray, rgba: np.ndarray, quad: np.ndarray) -> np.ndarray:
    h_c, w_c = rgba.shape[:2]

    quad_w = max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3]))
    quad_h = max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1]))
    target_w = max(1, int(round(quad_w)))
    target_h = max(1, int(round(quad_h)))

    if target_w < w_c or target_h < h_c:
        rgba = cv2.resize(rgba, (target_w, target_h), interpolation=cv2.INTER_AREA)
        h_c, w_c = rgba.shape[:2]

    src_pts = np.array([[0, 0], [w_c - 1, 0], [w_c - 1, h_c - 1], [0, h_c - 1]], dtype=np.float32)
    dst_pts = quad.astype(np.float32)
    matrix = cv2.getPerspectiveTransform(src_pts, dst_pts)

    out_h, out_w = base_rgb.shape[:2]
    warped = cv2.warpPerspective(
        rgba,
        matrix,
        (out_w, out_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )

    alpha = warped[..., 3:4].astype(np.float64) / 255.0
    result = base_rgb.astype(np.float64) * (1 - alpha) + warped[..., :3].astype(np.float64) * alpha
    return np.clip(result, 0, 255).astype(np.uint8)


def composite_print(
    print_img: np.ndarray,
    template_img: np.ndarray,
    region: TemplateRegion,
    cfg: MockupConfig,
    *,
    mat: bool = True,
    frame_inches: float | None = None,
) -> np.ndarray:
    """Composites print_img (fit + optional mat + optional frame) into
    region of template_img: light-match, shadow-under, perspective warp,
    alpha-composite. Returns a new array shaped like template_img."""
    photo_h, photo_w = print_img.shape[:2]
    print_w_in, print_h_in = print_rect_inches(photo_w, photo_h, region, cfg)
    ppi = region_ppi(region)

    composed = print_img
    total_w_in, total_h_in = print_w_in, print_h_in

    if mat:
        composed = apply_mat(composed, cfg.render.mat_fraction, cfg.render.mat_color)
        short_edge_in = min(print_w_in, print_h_in)
        mat_border_in = cfg.render.mat_fraction * short_edge_in
        total_w_in += 2 * mat_border_in
        total_h_in += 2 * mat_border_in

    if frame_inches is not None:
        frame_px = frame_inches * ppi
        composed = draw_frame_border(composed, frame_px, cfg.render.frame_color)
        total_w_in += 2 * frame_inches
        total_h_in += 2 * frame_inches

    quad = target_quad(region, total_w_in, total_h_in)

    lm = cfg.render.light_match
    gain, wb_r, wb_b = light_stats(template_img, quad, cfg) if lm.enabled else (1.0, 1.0, 1.0)
    lit = _apply_light_match(composed, gain, wb_r, wb_b)

    shadowed = draw_shadow(template_img, quad, cfg.render.shadow)

    h_c, w_c = lit.shape[:2]
    rgba = np.dstack([lit, np.full((h_c, w_c), 255, dtype=np.uint8)])

    return _warp_rgba_onto(shadowed, rgba, quad)


def finalize(img: np.ndarray, cfg: MockupConfig) -> Image.Image:
    """Resizes the long edge to cfg.render.output_long_edge_px and returns
    a plain RGB PIL Image (drops alpha if present)."""
    rgb_arr = img[..., :3] if img.ndim == 3 and img.shape[2] == 4 else img
    pil = Image.fromarray(rgb_arr, mode="RGB")

    long_edge = max(pil.size)
    target = cfg.render.output_long_edge_px
    if long_edge != target:
        scale = target / long_edge
        new_size = (max(1, round(pil.size[0] * scale)), max(1, round(pil.size[1] * scale)))
        pil = pil.resize(new_size, Image.LANCZOS)
    return pil.convert("RGB")
