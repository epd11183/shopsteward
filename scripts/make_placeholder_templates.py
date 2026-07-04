"""Generates 4 synthetic room-scene placeholders (JPEG + `.template.json`
sidecar each) into config/defaults/staging_templates/. Standalone: stdlib +
Pillow only, no shopsteward imports, so it can run without the package
installed. Re-run to regenerate deterministically; outputs are committed.

Usage: uv run python scripts/make_placeholder_templates.py
"""

import json
from pathlib import Path

from PIL import Image, ImageDraw

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OUT_DIR = _REPO_ROOT / "config" / "defaults" / "staging_templates"

_SCHEMA = "shopsteward.stagingtemplate/1"
_JPEG_QUALITY = 80


def _draw_room(
    size: tuple[int, int],
    *,
    wall_color: tuple[int, int, int],
    floor_top_frac: float,
    with_sofa: bool,
) -> Image.Image:
    width, height = size
    img = Image.new("RGB", size, wall_color)
    draw = ImageDraw.Draw(img)

    floor_top = int(height * floor_top_frac)
    floor_color = tuple(max(0, c - 60) for c in wall_color)
    draw.rectangle([0, floor_top, width, height], fill=floor_color)

    baseboard_color = tuple(min(255, c + 20) for c in wall_color)
    draw.rectangle([0, floor_top - 8, width, floor_top], fill=baseboard_color)

    if with_sofa:
        sofa_w = int(width * 0.34)
        sofa_h = int(height * 0.14)
        sofa_x = int(width * 0.10)
        sofa_y = floor_top - sofa_h
        sofa_color = tuple(max(0, c - 90) for c in wall_color)
        draw.rectangle([sofa_x, sofa_y, sofa_x + sofa_w, sofa_y + sofa_h], fill=sofa_color)

    return img


def _sidecar(
    *,
    template_id: str,
    room_type: str,
    style: str,
    lighting: str,
    orientation: str,
    regions: list[dict],
    tags: list[str],
) -> dict:
    return {
        "schema": _SCHEMA,
        "template_id": template_id,
        "room_type": room_type,
        "style": style,
        "lighting": lighting,
        "orientation": orientation,
        "regions": regions,
        "tags": tags,
    }


def _write(stem: str, image: Image.Image, sidecar: dict) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    image_path = _OUT_DIR / f"{stem}.jpg"
    sidecar_path = _OUT_DIR / f"{stem}.template.json"
    image.convert("RGB").save(image_path, "JPEG", quality=_JPEG_QUALITY)
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n")
    print(f"wrote {image_path.relative_to(_REPO_ROOT)} + {sidecar_path.relative_to(_REPO_ROOT)}")


def main() -> None:
    # 1. Living room, warm beige, landscape, single region, sofa.
    img1 = _draw_room((1600, 1200), wall_color=(230, 215, 195), floor_top_frac=0.72, with_sofa=True)
    _write(
        "livingroom-warm-01",
        img1,
        _sidecar(
            template_id="livingroom-warm-01",
            room_type="living_room",
            style="modern",
            lighting="warm_daylight",
            orientation="landscape",
            regions=[
                {
                    "kind": "wall_print",
                    "quad": [[300.0, 200.0], [1300.0, 215.0], [1290.0, 800.0], [310.0, 785.0]],
                    "region_width_inches": 36.0,
                }
            ],
            tags=["neutral_wall", "sofa", "warm"],
        ),
    )

    # 2. Bedroom, cool gray, portrait, single region, no furniture.
    img2 = _draw_room(
        (1200, 1600), wall_color=(200, 205, 210), floor_top_frac=0.82, with_sofa=False
    )
    _write(
        "bedroom-cool-01",
        img2,
        _sidecar(
            template_id="bedroom-cool-01",
            room_type="bedroom",
            style="scandinavian",
            lighting="cool_daylight",
            orientation="portrait",
            regions=[
                {
                    "kind": "wall_print",
                    "quad": [[150.0, 150.0], [1050.0, 160.0], [1040.0, 1300.0], [160.0, 1290.0]],
                    "region_width_inches": 24.0,
                }
            ],
            tags=["neutral_wall", "minimal"],
        ),
    )

    # 3. Office, sage green, landscape, single region, no furniture.
    img3 = _draw_room(
        (1600, 1200), wall_color=(170, 185, 160), floor_top_frac=0.70, with_sofa=False
    )
    _write(
        "office-sage-01",
        img3,
        _sidecar(
            template_id="office-sage-01",
            room_type="office",
            style="minimalist",
            lighting="neutral",
            orientation="landscape",
            regions=[
                {
                    "kind": "wall_print",
                    "quad": [[350.0, 220.0], [1250.0, 230.0], [1245.0, 780.0], [355.0, 770.0]],
                    "region_width_inches": 30.0,
                }
            ],
            tags=["neutral_wall", "office", "sage"],
        ),
    )

    # 4. Living room, charcoal, landscape, two regions (gallery wall), sofa.
    img4 = _draw_room((1600, 1200), wall_color=(60, 58, 56), floor_top_frac=0.74, with_sofa=True)
    _write(
        "gallery-charcoal-01",
        img4,
        _sidecar(
            template_id="gallery-charcoal-01",
            room_type="living_room",
            style="industrial",
            lighting="moody",
            orientation="landscape",
            regions=[
                {
                    "kind": "wall_print",
                    "quad": [[200.0, 250.0], [750.0, 260.0], [745.0, 750.0], [205.0, 740.0]],
                    "region_width_inches": 24.0,
                },
                {
                    "kind": "wall_print",
                    "quad": [[850.0, 270.0], [1250.0, 275.0], [1246.0, 730.0], [854.0, 725.0]],
                    "region_width_inches": 16.0,
                },
            ],
            tags=["gallery_wall", "charcoal", "sofa"],
        ),
    )


if __name__ == "__main__":
    main()
