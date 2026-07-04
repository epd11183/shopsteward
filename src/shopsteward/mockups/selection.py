"""Pure template selection: photo_avg_hue (same method as
templates._compute_avg_hue) + select_templates (eligibility, hue ranking,
greedy room-type diversity with relax, lex tiebreak). No DB, no events."""

import numpy as np
from PIL import Image

_HUE_THUMBNAIL_PX = 64


def photo_avg_hue(photo: np.ndarray) -> float:
    """Mean hue (degrees, 0-360) of a 64x64 thumbnail -- mirrors
    templates._compute_avg_hue's PIL-HSV method exactly, but from an
    in-memory array instead of an image path."""
    rgb = photo[..., :3] if photo.ndim == 3 and photo.shape[2] == 4 else photo
    img = Image.fromarray(rgb, mode="RGB")
    img.thumbnail((_HUE_THUMBNAIL_PX, _HUE_THUMBNAIL_PX))
    hsv = img.convert("HSV")
    h_channel = hsv.getchannel("H")
    hues = list(h_channel.get_flattened_data())
    mean_hue_255 = sum(hues) / len(hues)
    return mean_hue_255 * 360.0 / 255.0


def _hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def select_templates(
    photo_hue: float,
    photo_orientation: str,
    templates: list[dict],
    intent: str,
    count: int,
    used_room_types: set[str],
) -> list[dict]:
    """Eligible: orientation in {photo's, "any"}; gallery_wall additionally
    requires region_count >= 2 (single/framed_poster fit any region_count,
    using the largest region). Ranked by circular hue distance, template_id
    lex tiebreak. Greedy diversity: skip a room_type already present in
    used_room_types, relaxed once eligible candidates without a fresh
    room_type run out. Returns fewer than count if templates run out."""
    eligible = [
        t
        for t in templates
        if t["orientation"] in (photo_orientation, "any")
        and (intent != "gallery_wall" or t["region_count"] >= 2)
    ]

    ranked = sorted(
        eligible, key=lambda t: (_hue_distance(photo_hue, t["avg_hue"]), t["template_id"])
    )

    selected: list[dict] = []
    used = set(used_room_types)
    remaining = list(ranked)

    while remaining and len(selected) < count:
        candidate = next((t for t in remaining if t["room_type"] not in used), None)
        if candidate is None:
            candidate = remaining[0]  # diversity exhausted -- relax, allow repeats
        selected.append(candidate)
        used.add(candidate["room_type"])
        remaining.remove(candidate)

    return selected
