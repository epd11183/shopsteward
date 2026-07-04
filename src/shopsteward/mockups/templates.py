"""Staging template registry scan (mirrors pipeline/landing.py): merges
config/defaults/staging_templates/ + the operator templates dir (+ extra_dirs
for tests), validates `<image_stem>.template.json` sidecars, computes
avg_hue, emits stagingtemplate.* events. Sidecars are file-authoritative;
scan is idempotent by sidecar_hash + image_hash.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.models import StagingTemplate, TemplateRegion, TemplateReport
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.settings import operator_templates_dir

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEMPLATES_DIR = _REPO_ROOT / "config" / "defaults" / "staging_templates"

_SIDECAR_SUFFIX = ".template.json"
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
_ORIENTATIONS = {"landscape", "portrait", "square", "any"}
_MIN_AREA_FRACTION = 0.015
_MIN_EDGE_PX = 40
_WIDTH_INCHES_RANGE = (6.0, 120.0)
_PPI_RANGE = (10.0, 300.0)
_HUE_THUMBNAIL_PX = 64


def _sidecar_stem(sidecar_path: Path) -> str | None:
    name = sidecar_path.name
    if not name.endswith(_SIDECAR_SUFFIX):
        return None
    return name[: -len(_SIDECAR_SUFFIX)]


def _find_paired_image(sidecar_path: Path) -> Path | None:
    stem = _sidecar_stem(sidecar_path)
    if stem is None:
        return None
    for suffix in _IMAGE_SUFFIXES:
        candidate = sidecar_path.parent / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _validate_region(region: TemplateRegion, width: int, height: int) -> str | None:
    quad = region.quad
    if len(quad) != 4 or any(len(pt) != 2 for pt in quad):
        return "quad_invalid_points"

    pts = [(float(x), float(y)) for x, y in quad]

    for x, y in pts:
        if x < 0 or y < 0 or x > width or y > height:
            return "quad_out_of_bounds"

    cross_signs = []
    for i in range(4):
        p0, p1, p2 = pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]
        v1 = (p1[0] - p0[0], p1[1] - p0[1])
        v2 = (p2[0] - p1[0], p2[1] - p1[1])
        cross_signs.append(v1[0] * v2[1] - v1[1] * v2[0])
    if any(c == 0 for c in cross_signs):
        return "quad_concave"
    if not (all(c > 0 for c in cross_signs) or all(c < 0 for c in cross_signs)):
        return "quad_concave"

    area = 0.0
    for i in range(4):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % 4]
        area += x1 * y2 - x2 * y1
    area = abs(area) / 2
    if area < _MIN_AREA_FRACTION * width * height:
        return "quad_too_small"

    def edge_len(a: tuple[float, float], b: tuple[float, float]) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    edges = [edge_len(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    if min(edges) < _MIN_EDGE_PX:
        return "quad_edge_too_short"

    lo, hi = _WIDTH_INCHES_RANGE
    if not (lo <= region.region_width_inches <= hi):
        return "region_width_out_of_range"

    top_edge_px = edges[0]  # TL -> TR
    implied_ppi = top_edge_px / region.region_width_inches
    lo, hi = _PPI_RANGE
    if not (lo <= implied_ppi <= hi):
        return "implied_ppi_out_of_range"

    return None


def _validate_geometry(template: StagingTemplate, width: int, height: int) -> str | None:
    if template.orientation not in _ORIENTATIONS:
        return "orientation_invalid"
    for region in template.regions:
        reason = _validate_region(region, width, height)
        if reason is not None:
            return reason
    return None


def _compute_avg_hue(image_path: Path) -> float:
    with Image.open(image_path) as img:
        rgb = img.convert("RGB")
        rgb.thumbnail((_HUE_THUMBNAIL_PX, _HUE_THUMBNAIL_PX))
        hsv = rgb.convert("HSV")
        h_channel = hsv.getchannel("H")
        hues = list(h_channel.get_flattened_data())
    mean_hue_255 = sum(hues) / len(hues)
    return mean_hue_255 * 360.0 / 255.0


def _known_state(conn: sqlite3.Connection, user_id: int) -> dict[str, dict]:
    state: dict[str, dict] = {}
    for e in read_all(conn, "stagingtemplate."):
        if e.user_id != user_id:
            continue
        p = e.payload
        if e.type in ("stagingtemplate.registered", "stagingtemplate.updated"):
            state[p["template_id"]] = {
                "status": "valid",
                "sidecar_hash": p["sidecar_hash"],
                "image_hash": p["image_hash"],
            }
        elif e.type == "stagingtemplate.invalid":
            # Keyed by sidecar_path (not template_id): two sidecars sharing a
            # duplicate template_id are still distinct files and must each be
            # tracked/re-emitted independently.
            key = f"invalid:{p['sidecar_path']}"
            state[key] = {
                "status": "invalid",
                "sidecar_hash": p["sidecar_hash"],
                "reason": p["reason"],
            }
    return state


def _sidecar_files(dirs: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    found = []
    for dir_path, source in dirs:
        if not dir_path.is_dir():
            continue
        for f in sorted(dir_path.glob(f"*{_SIDECAR_SUFFIX}")):
            if f.is_file():
                found.append((f, source))
    return found


def scan_templates(
    conn: sqlite3.Connection, user_id: int, extra_dirs: list[Path] | None = None
) -> TemplateReport:
    dirs = [(DEFAULT_TEMPLATES_DIR, "defaults"), (operator_templates_dir(), "operator")]
    for d in extra_dirs or []:
        dirs.append((Path(d), "operator"))

    parsed = []
    for sidecar_path, source in _sidecar_files(dirs):
        sidecar_bytes = sidecar_path.read_bytes()
        sidecar_hash = hashlib.sha256(sidecar_bytes).hexdigest()
        try:
            data = json.loads(sidecar_bytes)
            template = StagingTemplate.model_validate(data)
            reason = None
        except (json.JSONDecodeError, ValidationError):
            template = None
            reason = "invalid_schema"
        parsed.append(
            {
                "sidecar_path": sidecar_path,
                "source": source,
                "sidecar_hash": sidecar_hash,
                "template": template,
                "reason": reason,
            }
        )

    # Duplicate template_id across dirs -> both invalid (checked before
    # geometry/image validation so collisions always win with duplicate_id).
    ids_seen: dict[str, list[dict]] = {}
    for entry in parsed:
        if entry["template"] is None:
            continue
        ids_seen.setdefault(entry["template"].template_id, []).append(entry)
    for entries in ids_seen.values():
        if len(entries) > 1:
            for entry in entries:
                entry["reason"] = "duplicate_id"

    for entry in parsed:
        if entry["reason"] is not None or entry["template"] is None:
            continue
        image_path = _find_paired_image(entry["sidecar_path"])
        if image_path is None:
            entry["reason"] = "image_missing"
            continue
        with Image.open(image_path) as img:
            width, height = img.size
        geometry_reason = _validate_geometry(entry["template"], width, height)
        if geometry_reason is not None:
            entry["reason"] = geometry_reason
            continue
        entry["image_path"] = image_path

    known = _known_state(conn, user_id)
    report = TemplateReport()

    for entry in parsed:
        template = entry["template"]
        sidecar_path = entry["sidecar_path"]
        sidecar_hash = entry["sidecar_hash"]
        reason = entry["reason"]
        template_id = template.template_id if template is not None else None

        if reason is not None:
            report.invalid += 1
            key = f"invalid:{sidecar_path}"
            prior = known.get(key)
            if (
                prior is None
                or prior.get("status") != "invalid"
                or prior.get("sidecar_hash") != sidecar_hash
                or prior.get("reason") != reason
            ):
                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="stagingtemplate.invalid",
                        payload={
                            "template_id": template_id,
                            "sidecar_path": str(sidecar_path),
                            "sidecar_hash": sidecar_hash,
                            "reason": reason,
                        },
                    ),
                )
                known[key] = {"status": "invalid", "sidecar_hash": sidecar_hash, "reason": reason}
            continue

        image_path: Path = entry["image_path"]
        image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
        avg_hue = _compute_avg_hue(image_path)

        prior = known.get(template_id)
        payload = {
            "template_id": template_id,
            "image_path": str(image_path),
            "sidecar_path": str(sidecar_path),
            "sidecar_hash": sidecar_hash,
            "image_hash": image_hash,
            "room_type": template.room_type,
            "style": template.style,
            "lighting": template.lighting,
            "orientation": template.orientation,
            "region_count": len(template.regions),
            "region_width_inches": [r.region_width_inches for r in template.regions],
            "tags": template.tags,
            "avg_hue": avg_hue,
            "source": entry["source"],
        }

        if prior is None or prior.get("status") != "valid":
            append(conn, Event(user_id=user_id, type="stagingtemplate.registered", payload=payload))
            known[template_id] = {
                "status": "valid",
                "sidecar_hash": sidecar_hash,
                "image_hash": image_hash,
            }
            report.registered += 1
        elif prior.get("sidecar_hash") != sidecar_hash or prior.get("image_hash") != image_hash:
            append(conn, Event(user_id=user_id, type="stagingtemplate.updated", payload=payload))
            known[template_id] = {
                "status": "valid",
                "sidecar_hash": sidecar_hash,
                "image_hash": image_hash,
            }
            report.updated += 1
        else:
            report.unchanged += 1

    rebuild_mockups(conn)
    return report


def write_sidecar(image_path: Path, template: StagingTemplate, allowed_dirs: list[Path]) -> Path:
    """Writes `<image_stem>.template.json` next to image_path. Refuses to
    write outside of allowed_dirs (the annotate endpoint must only ever
    touch library directories)."""
    resolved_image = Path(image_path).resolve()
    if not any(resolved_image.is_relative_to(Path(d).resolve()) for d in allowed_dirs):
        raise ValueError(f"{resolved_image} is not under an allowed template directory")

    stem = resolved_image.stem
    sidecar_path = resolved_image.parent / f"{stem}{_SIDECAR_SUFFIX}"
    sidecar_path.write_text(json.dumps(template.model_dump(by_alias=True), indent=2))
    return sidecar_path
