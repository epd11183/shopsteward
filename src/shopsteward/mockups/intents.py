"""One render function per mockup intent. Template-backed intents (single,
gallery_wall, framed_poster) call compositor.composite_print(); synthetic
intents (canvas_edge, acrylic, digital_whatyougot) are pure Pillow/OpenCV
with no template. Pure functions -- no DB, no events, no network."""

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from shopsteward.mockups.compositor import (
    composite_print,
    draw_shadow,
    finalize,
    light_stats,
    photo_orientation,
    print_rect_inches,
    region_ppi,
    target_quad,
)
from shopsteward.mockups.models import MockupConfig, StagingTemplate, TemplateRegion

_SYNTHETIC_CANVAS_SIZE = (2400, 1800)  # (w, h)
_SYNTHETIC_BACKDROP_COLOR = (240, 239, 236)
_ACRYLIC_BACKDROP_COLOR = (250, 250, 250)
_WHATYOUGOT_PANEL_PX = 2400


def _synthetic_print_size_inches(
    photo_w: float, photo_h: float, cfg: MockupConfig
) -> tuple[float, float]:
    orientation = photo_orientation(photo_w, photo_h)
    w_in = cfg.products.default_print_widths_inches[orientation]
    h_in = w_in * (photo_h / photo_w)
    return w_in, h_in


def _region_area(region: TemplateRegion) -> float:
    pts = np.array(region.quad, dtype=float)
    x, y = pts[:, 0], pts[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _template_render_params(
    photo: np.ndarray,
    template_img: np.ndarray,
    region: TemplateRegion,
    cfg: MockupConfig,
    *,
    mat: bool,
    frame_inches: float | None,
) -> dict:
    photo_h, photo_w = photo.shape[:2]
    w_in, h_in = print_rect_inches(photo_w, photo_h, region, cfg)
    total_w_in, total_h_in = w_in, h_in

    if mat:
        short_edge_in = min(w_in, h_in)
        mat_border_in = cfg.render.mat_fraction * short_edge_in
        total_w_in += 2 * mat_border_in
        total_h_in += 2 * mat_border_in
    if frame_inches is not None:
        total_w_in += 2 * frame_inches
        total_h_in += 2 * frame_inches

    quad = target_quad(region, total_w_in, total_h_in)
    lm = cfg.render.light_match
    gain, wb_r, wb_b = light_stats(template_img, quad, cfg) if lm.enabled else (1.0, 1.0, 1.0)

    return {
        "print_w_in": round(w_in, 3),
        "print_h_in": round(h_in, 3),
        "gain": round(gain, 3),
        "wb_r": round(wb_r, 3),
        "wb_b": round(wb_b, 3),
    }


def render_single(
    photo: np.ndarray,
    template: StagingTemplate,
    template_img: np.ndarray,
    cfg: MockupConfig,
    region_index: int = 0,
) -> tuple[Image.Image, dict]:
    region = template.regions[region_index]
    composed = composite_print(photo, template_img, region, cfg, mat=True, frame_inches=0.25)
    out = finalize(composed, cfg)

    params = {"intent": "single", "template_id": template.template_id}
    params.update(
        _template_render_params(photo, template_img, region, cfg, mat=True, frame_inches=0.25)
    )
    return out, params


def render_framed_poster(
    photo: np.ndarray,
    template: StagingTemplate,
    template_img: np.ndarray,
    cfg: MockupConfig,
    region_index: int = 0,
) -> tuple[Image.Image, dict]:
    region = template.regions[region_index]
    frame_inches = cfg.render.frame_width_inches
    composed = composite_print(
        photo, template_img, region, cfg, mat=True, frame_inches=frame_inches
    )
    out = finalize(composed, cfg)

    params = {"intent": "framed_poster", "template_id": template.template_id}
    params.update(
        _template_render_params(
            photo, template_img, region, cfg, mat=True, frame_inches=frame_inches
        )
    )
    return out, params


def _crop_for_region(photo: np.ndarray, region: TemplateRegion, crop_index: int) -> np.ndarray:
    """Deterministic crop of photo at region's aspect: crop_index 0 = center,
    odd/even indices thereafter alternate rule-of-thirds horizontal offsets."""
    ph, pw = photo.shape[:2]
    ppi = region_ppi(region)
    region_w_in = region.region_width_inches
    quad = np.array(region.quad, dtype=float)
    region_h_in = (
        float(np.linalg.norm(quad[3] - quad[0]) + np.linalg.norm(quad[2] - quad[1])) / 2 / ppi
    )
    target_aspect = region_w_in / region_h_in
    photo_aspect = pw / ph

    if photo_aspect > target_aspect:
        crop_h = ph
        crop_w = min(pw, int(round(ph * target_aspect)))
    else:
        crop_w = pw
        crop_h = min(ph, int(round(pw / target_aspect)))

    max_x, max_y = pw - crop_w, ph - crop_h
    if crop_index == 0:
        x0 = max_x // 2
    else:
        frac = 1 / 3 if crop_index % 2 == 1 else 2 / 3
        x0 = int(round(max_x * frac))
    y0 = max_y // 2
    return photo[y0 : y0 + crop_h, x0 : x0 + crop_w]


def render_gallery_wall(
    photo: np.ndarray,
    template: StagingTemplate,
    template_img: np.ndarray,
    cfg: MockupConfig,
    companion_photos: list[np.ndarray] | None = None,
) -> tuple[Image.Image, dict]:
    region_order = sorted(
        range(len(template.regions)), key=lambda i: -_region_area(template.regions[i])
    )
    largest_idx = region_order[0]
    other_indices = region_order[1:]

    largest_region = template.regions[largest_idx]
    composed = composite_print(
        photo, template_img, largest_region, cfg, mat=True, frame_inches=None
    )
    crop_notes = [{"region_index": largest_idx, "crop": "full_fit"}]

    for crop_index, region_idx in enumerate(other_indices):
        region = template.regions[region_idx]
        if companion_photos and crop_index < len(companion_photos):
            source_photo = companion_photos[crop_index]
            crop_kind = "companion"
        else:
            source_photo = _crop_for_region(photo, region, crop_index)
            crop_kind = "center_crop" if crop_index == 0 else "rule_of_thirds"
        composed = composite_print(source_photo, composed, region, cfg, mat=True, frame_inches=None)
        crop_notes.append({"region_index": region_idx, "crop": crop_kind})

    out = finalize(composed, cfg)
    photo_h, photo_w = photo.shape[:2]
    w_in, h_in = print_rect_inches(photo_w, photo_h, largest_region, cfg)
    params = {
        "intent": "gallery_wall",
        "template_id": template.template_id,
        "print_w_in": round(w_in, 3),
        "print_h_in": round(h_in, 3),
        "crops": crop_notes,
    }
    return out, params


def render_canvas_edge(photo: np.ndarray, cfg: MockupConfig) -> tuple[Image.Image, dict]:
    """Synthetic 3/4-view canvas wrap: front face at a fixed ~12deg yaw, side
    face = mirrored strip of the photo's right edge, darkened by a vertical
    gradient, soft shadow under both faces. No template."""
    canvas_w, canvas_h = _SYNTHETIC_CANVAS_SIZE
    canvas = np.full((canvas_h, canvas_w, 3), _SYNTHETIC_BACKDROP_COLOR, dtype=np.uint8)

    ph, pw = photo.shape[:2]
    photo_aspect = pw / ph

    front_w = int(canvas_w * 0.62)
    front_h = min(int(round(front_w / photo_aspect)), int(canvas_h * 0.7))
    front_w = int(round(front_h * photo_aspect))

    margin_x = int(canvas_w * 0.12)
    margin_y = (canvas_h - front_h) // 2

    yaw_px = max(2, int(round(front_h * np.tan(np.deg2rad(12)) * 0.15)))
    front_quad = np.array(
        [
            [margin_x, margin_y],
            [margin_x + front_w, margin_y + yaw_px],
            [margin_x + front_w, margin_y + front_h - yaw_px],
            [margin_x, margin_y + front_h],
        ],
        dtype=np.float32,
    )

    print_w_in, _ = _synthetic_print_size_inches(pw, ph, cfg)
    side_depth_in = cfg.render.canvas_wrap_depth_inches
    side_w_px = max(4, int(round(side_depth_in * (front_w / print_w_in))))
    side_inset = int(side_w_px * 0.3)

    side_quad = np.array(
        [
            [margin_x + front_w, margin_y + yaw_px],
            [margin_x + front_w + side_w_px, margin_y + yaw_px + side_inset],
            [margin_x + front_w + side_w_px, margin_y + front_h - yaw_px - side_inset],
            [margin_x + front_w, margin_y + front_h - yaw_px],
        ],
        dtype=np.float32,
    )

    combined_quad = np.array(
        [front_quad[0], side_quad[1], side_quad[2], front_quad[3]], dtype=np.float32
    )
    canvas = draw_shadow(canvas, combined_quad, cfg.render.shadow)

    src = np.array([[0, 0], [pw - 1, 0], [pw - 1, ph - 1], [0, ph - 1]], dtype=np.float32)
    matrix_front = cv2.getPerspectiveTransform(src, front_quad)
    front_warp = cv2.warpPerspective(
        photo, matrix_front, (canvas_w, canvas_h), flags=cv2.INTER_LINEAR
    )
    front_mask = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    cv2.fillConvexPoly(front_mask, front_quad.astype(np.int32), 255)
    front_alpha = front_mask > 0
    canvas[front_alpha] = front_warp[front_alpha]

    strip_src_w = max(1, int(pw * 0.06))
    strip = photo[:, pw - strip_src_w : pw]
    strip_mirrored = strip[:, ::-1]
    strip_h, strip_w = strip_mirrored.shape[:2]
    src_side = np.array(
        [[0, 0], [strip_w - 1, 0], [strip_w - 1, strip_h - 1], [0, strip_h - 1]], dtype=np.float32
    )
    matrix_side = cv2.getPerspectiveTransform(src_side, side_quad)
    side_warp = cv2.warpPerspective(
        strip_mirrored, matrix_side, (canvas_w, canvas_h), flags=cv2.INTER_LINEAR
    )

    gradient = np.linspace(0.75, 0.6, canvas_h).reshape(-1, 1, 1)
    side_warp = np.clip(side_warp.astype(np.float64) * gradient, 0, 255).astype(np.uint8)

    side_mask = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    cv2.fillConvexPoly(side_mask, side_quad.astype(np.int32), 255)
    side_alpha = side_mask > 0
    canvas[side_alpha] = side_warp[side_alpha]

    out = finalize(canvas, cfg)
    w_in, h_in = _synthetic_print_size_inches(pw, ph, cfg)
    params = {
        "intent": "canvas_edge",
        "template_id": None,
        "print_w_in": round(w_in, 3),
        "print_h_in": round(h_in, 3),
    }
    return out, params


def _ensure_odd(value: int) -> int:
    return value if value % 2 == 1 else value + 1


def render_acrylic(photo: np.ndarray, cfg: MockupConfig) -> tuple[Image.Image, dict]:
    """Synthetic acrylic print: photo flat on a neutral backdrop with a 6px
    white polished edge, a large soft standoff shadow, and a fixed diagonal
    gloss band. No template."""
    canvas_w, canvas_h = _SYNTHETIC_CANVAS_SIZE
    canvas = np.full((canvas_h, canvas_w, 3), _ACRYLIC_BACKDROP_COLOR, dtype=np.uint8)

    ph, pw = photo.shape[:2]
    aspect = pw / ph
    max_w, max_h = int(canvas_w * 0.7), int(canvas_h * 0.7)
    if max_w / aspect <= max_h:
        disp_w, disp_h = max_w, int(round(max_w / aspect))
    else:
        disp_h, disp_w = max_h, int(round(max_h * aspect))

    x0, y0 = (canvas_w - disp_w) // 2, (canvas_h - disp_h) // 2
    border = 6

    shadow_offset = int(round(0.025 * canvas_w))
    shadow_mask = np.zeros((canvas_h, canvas_w), dtype=np.uint8)
    cv2.rectangle(
        shadow_mask,
        (x0 - border + shadow_offset, y0 - border + shadow_offset),
        (x0 + disp_w + border + shadow_offset, y0 + disp_h + border + shadow_offset),
        255,
        -1,
    )
    blur_px = _ensure_odd(int(round(0.06 * canvas_w)))
    shadow_blur = cv2.GaussianBlur(shadow_mask, (blur_px, blur_px), 0)
    shadow_alpha = (shadow_blur.astype(np.float64) / 255.0) * 0.35
    canvas = np.clip(canvas.astype(np.float64) * (1 - shadow_alpha[..., None]), 0, 255).astype(
        np.uint8
    )

    cv2.rectangle(
        canvas,
        (x0 - border, y0 - border),
        (x0 + disp_w + border, y0 + disp_h + border),
        (255, 255, 255),
        -1,
    )
    resized_photo = cv2.resize(photo, (disp_w, disp_h), interpolation=cv2.INTER_AREA)
    canvas[y0 : y0 + disp_h, x0 : x0 + disp_w] = resized_photo

    yy, xx = np.mgrid[0:canvas_h, 0:canvas_w]
    diag = (xx.astype(np.float64) / canvas_w + yy.astype(np.float64) / canvas_h) / 2.0
    band_center, band_width = 0.35, 0.18
    dist = np.abs(diag - band_center)
    gloss_alpha = np.clip(1 - dist / band_width, 0, 1) * 0.08
    canvas = np.clip(canvas.astype(np.float64) + gloss_alpha[..., None] * 255.0, 0, 255).astype(
        np.uint8
    )

    out = finalize(canvas, cfg)
    w_in, h_in = _synthetic_print_size_inches(pw, ph, cfg)
    params = {
        "intent": "acrylic",
        "template_id": None,
        "print_w_in": round(w_in, 3),
        "print_h_in": round(h_in, 3),
    }
    return out, params


def render_digital_whatyougot(photo: np.ndarray, cfg: MockupConfig) -> tuple[Image.Image, dict]:
    """2400x2400 white panel: photo thumb top-left, headline, size chips,
    formats line. No template."""
    panel = Image.new("RGB", (_WHATYOUGOT_PANEL_PX, _WHATYOUGOT_PANEL_PX), (255, 255, 255))
    draw = ImageDraw.Draw(panel)

    rgb = photo[..., :3] if photo.ndim == 3 and photo.shape[2] == 4 else photo
    photo_img = Image.fromarray(rgb, mode="RGB")
    thumb_w = int(_WHATYOUGOT_PANEL_PX * 0.4)
    thumb_h = max(1, int(round(thumb_w * photo_img.height / photo_img.width)))
    thumb = photo_img.resize((thumb_w, thumb_h), Image.LANCZOS)

    margin = 80
    panel.paste(thumb, (margin, margin))

    font_headline = ImageFont.load_default(size=64)
    font_body = ImageFont.load_default(size=40)

    headline_x = margin * 2 + thumb_w
    headline_y = margin
    draw.text(
        (headline_x, headline_y), cfg.whatyougot.headline, fill=(20, 20, 20), font=font_headline
    )

    chip_x, chip_y = headline_x, headline_y + 100
    chip_h, chip_gap = 70, 20
    x, y = chip_x, chip_y
    for size_label in cfg.whatyougot.sizes:
        bbox = draw.textbbox((0, 0), size_label, font=font_body)
        text_w = bbox[2] - bbox[0]
        chip_w = text_w + 48
        if x + chip_w > _WHATYOUGOT_PANEL_PX - margin:
            x = chip_x
            y += chip_h + chip_gap
        draw.rounded_rectangle(
            [x, y, x + chip_w, y + chip_h], radius=16, outline=(60, 60, 60), width=2
        )
        draw.text((x + 24, y + 16), size_label, fill=(30, 30, 30), font=font_body)
        x += chip_w + chip_gap

    formats_y = max(y + chip_h + 60, margin + thumb_h + 60)
    formats_line = " · ".join(cfg.whatyougot.formats)
    draw.text((margin, formats_y), formats_line, fill=(80, 80, 80), font=font_body)

    ph, pw = photo.shape[:2]
    w_in, h_in = _synthetic_print_size_inches(pw, ph, cfg)
    params = {
        "intent": "digital_whatyougot",
        "template_id": None,
        "print_w_in": round(w_in, 3),
        "print_h_in": round(h_in, 3),
    }
    return panel, params


def render_intent(
    intent: str,
    photo: np.ndarray,
    template: StagingTemplate | None,
    template_img: np.ndarray | None,
    cfg: MockupConfig,
    region_index: int = 0,
    companion_photos: list[np.ndarray] | None = None,
) -> tuple[Image.Image, dict]:
    if intent == "single":
        return render_single(photo, template, template_img, cfg, region_index)
    if intent == "framed_poster":
        return render_framed_poster(photo, template, template_img, cfg, region_index)
    if intent == "gallery_wall":
        return render_gallery_wall(photo, template, template_img, cfg, companion_photos)
    if intent == "canvas_edge":
        return render_canvas_edge(photo, cfg)
    if intent == "acrylic":
        return render_acrylic(photo, cfg)
    if intent == "digital_whatyougot":
        return render_digital_whatyougot(photo, cfg)
    raise ValueError(f"unknown intent: {intent}")
