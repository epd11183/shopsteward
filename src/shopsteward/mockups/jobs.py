"""Mockup job orchestration: eligible landing files -> select templates ->
render -> write data/mockups/... -> mockup.generated + mockupset.completed.

Idempotent by set_key = sha256(landing_file_id | config_hash | template_library_hash);
--force bypasses. No DB/network in the render path -- all rendering is the
pure functions in compositor.py/intents.py/selection.py, this module only
owns I/O, selection bookkeeping, and event emission.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from shopsteward.core.events import Event, append
from shopsteward.editing.projections import rebuild_editing
from shopsteward.mockups import compositor, selection
from shopsteward.mockups.config import config_hash, load_mockup_defaults
from shopsteward.mockups.intents import render_intent
from shopsteward.mockups.models import MockupConfig, MockupJobResult, StagingTemplate
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.mockups.templates import scan_templates
from shopsteward.pipeline.projections import rebuild_pipeline
from shopsteward.settings import mockups_dir

_TEMPLATE_BACKED_INTENTS = {"single", "gallery_wall", "framed_poster"}

# Photo masters are prepped once and reused across every intent for a photo;
# this cap keeps memory bounded on large TIFFs while staying comfortably
# above any placeholder template's region size.
_MASTER_MAX_LONG_EDGE_PX = 6000


def _eligible_landing_rows(
    conn: sqlite3.Connection, user_id: int, photo_id: str | None
) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT file_id, path, photo_id FROM proj_landing_files "
        "WHERE user_id=? AND status='valid' ORDER BY file_id",
        (user_id,),
    ).fetchall()
    if photo_id is None:
        return rows
    return [
        row
        for row in rows
        if row["photo_id"] == photo_id
        or (row["photo_id"] is None and f"file-{row['file_id'][:12]}" == photo_id)
    ]


def _template_library_hash(conn: sqlite3.Connection, user_id: int) -> str:
    rows = conn.execute(
        "SELECT template_id, sidecar_hash, image_hash FROM proj_staging_templates "
        "WHERE user_id=? AND status='valid' ORDER BY template_id",
        (user_id,),
    ).fetchall()
    canonical = "|".join(f"{r['template_id']}:{r['sidecar_hash']}:{r['image_hash']}" for r in rows)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _candidate_templates(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT template_id, orientation, region_count, avg_hue, room_type, "
        "image_path, sidecar_path FROM proj_staging_templates "
        "WHERE user_id=? AND status='valid'",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_template_object(sidecar_path: str) -> StagingTemplate:
    data = json.loads(Path(sidecar_path).read_text())
    return StagingTemplate.model_validate(data)


def _load_template_image(image_path: str) -> np.ndarray:
    with Image.open(image_path) as img:
        return np.asarray(img.convert("RGB"), dtype=np.uint8)


def _region_area(quad: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(quad)):
        x1, y1 = quad[i]
        x2, y2 = quad[(i + 1) % len(quad)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2


def _largest_region_index(template: StagingTemplate) -> int:
    return max(
        range(len(template.regions)),
        key=lambda i: _region_area(template.regions[i].quad),
    )


def _event_params(region_index: int | None, render_params: dict, cfg: MockupConfig) -> dict:
    params: dict = {
        "print_w_in": render_params.get("print_w_in"),
        "print_h_in": render_params.get("print_h_in"),
        "mat_frac": cfg.render.mat_fraction,
        "out_px": cfg.render.output_long_edge_px,
    }
    if region_index is not None:
        params["quad_index"] = region_index
    gain = render_params.get("gain")
    if gain is not None:
        params["gain"] = gain
    wb_r, wb_b = render_params.get("wb_r"), render_params.get("wb_b")
    if wb_r is not None and wb_b is not None:
        params["wb"] = [wb_r, wb_b]
    return params


def _unique_path(out_dir: Path, stem: str, used: set[str]) -> Path:
    """Guards against an (unexpected) filename collision within one run --
    template_id-derived stems are unique per select_templates() call, but this
    keeps a second render from silently clobbering a sibling's file."""
    candidate = f"{stem}.jpg"
    suffix = 1
    while candidate in used:
        suffix += 1
        candidate = f"{stem}_r{suffix}.jpg"
    used.add(candidate)
    return out_dir / candidate


def run_mockups(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    photo_id: str | None = None,
    force: bool = False,
    extra_template_dirs: list[Path] | None = None,
    output_dir: Path | None = None,
) -> MockupJobResult:
    scan_templates(conn, user_id, extra_dirs=extra_template_dirs)
    rebuild_editing(conn)
    rebuild_pipeline(conn)
    rebuild_mockups(conn)

    cfg = load_mockup_defaults()
    cfg_hash = config_hash(cfg)
    template_library_hash = _template_library_hash(conn, user_id)
    templates = _candidate_templates(conn, user_id)

    templates_invalid = conn.execute(
        "SELECT COUNT(*) AS n FROM proj_staging_templates WHERE user_id=? AND status='invalid'",
        (user_id,),
    ).fetchone()["n"]

    base_out_dir = output_dir or mockups_dir()

    result = MockupJobResult(templates_invalid=templates_invalid)

    for row in _eligible_landing_rows(conn, user_id, photo_id):
        landing_file_id = row["file_id"]
        set_key = hashlib.sha256(
            f"{landing_file_id}|{cfg_hash}|{template_library_hash}".encode()
        ).hexdigest()

        existing = conn.execute(
            "SELECT 1 FROM proj_mockup_sets WHERE user_id=? AND set_key=?",
            (user_id, set_key),
        ).fetchone()
        if existing and not force:
            result.skipped_idempotent += 1
            continue

        photo_ref = row["photo_id"] or f"file-{landing_file_id[:12]}"
        out_dir = Path(base_out_dir) / photo_ref
        out_dir.mkdir(parents=True, exist_ok=True)

        master = compositor.prep_master(Path(row["path"]), _MASTER_MAX_LONG_EDGE_PX)
        photo_hue = selection.photo_avg_hue(master)
        orientation = compositor.photo_orientation(master.shape[1], master.shape[0])

        used_room_types: set[str] = set()
        used_filenames: set[str] = set()
        files_written = 0

        for intent_name, intent_cfg in cfg.intents.items():
            if not intent_cfg.enabled:
                continue

            if intent_name in _TEMPLATE_BACKED_INTENTS:
                selected = selection.select_templates(
                    photo_hue,
                    orientation,
                    templates,
                    intent_name,
                    intent_cfg.count,
                    used_room_types,
                )
                if not selected:
                    result.intents_skipped_no_template += 1
                    continue

                for template_row in selected:
                    used_room_types.add(template_row["room_type"])
                    template_obj = _load_template_object(template_row["sidecar_path"])
                    template_img = _load_template_image(template_row["image_path"])

                    region_index = None
                    if intent_name != "gallery_wall":
                        region_index = _largest_region_index(template_obj)
                        out, render_params = render_intent(
                            intent_name,
                            master,
                            template_obj,
                            template_img,
                            cfg,
                            region_index=region_index,
                        )
                    else:
                        out, render_params = render_intent(
                            intent_name, master, template_obj, template_img, cfg
                        )

                    path = _unique_path(
                        out_dir, f"{intent_name}_{template_row['template_id']}", used_filenames
                    )
                    out.save(path, "JPEG", quality=cfg.render.jpeg_quality)

                    append(
                        conn,
                        Event(
                            user_id=user_id,
                            type="mockup.generated",
                            payload={
                                "photo_id": row["photo_id"],
                                "landing_file_id": landing_file_id,
                                "set_key": set_key,
                                "intent": intent_name,
                                "template_id": template_row["template_id"],
                                "path": str(path),
                                "params": _event_params(region_index, render_params, cfg),
                            },
                        ),
                    )
                    files_written += 1

            else:
                out, render_params = render_intent(intent_name, master, None, None, cfg)
                path = _unique_path(out_dir, f"{intent_name}_synthetic", used_filenames)
                out.save(path, "JPEG", quality=cfg.render.jpeg_quality)

                append(
                    conn,
                    Event(
                        user_id=user_id,
                        type="mockup.generated",
                        payload={
                            "photo_id": row["photo_id"],
                            "landing_file_id": landing_file_id,
                            "set_key": set_key,
                            "intent": intent_name,
                            "template_id": None,
                            "path": str(path),
                            "params": _event_params(None, render_params, cfg),
                        },
                    ),
                )
                files_written += 1

        append(
            conn,
            Event(
                user_id=user_id,
                type="mockupset.completed",
                payload={
                    "photo_id": row["photo_id"],
                    "landing_file_id": landing_file_id,
                    "set_key": set_key,
                    "count": files_written,
                    "config_hash": cfg_hash,
                    "template_library_hash": template_library_hash,
                },
            ),
        )
        result.sets_completed += 1
        result.mockups_written += files_written

    rebuild_mockups(conn)
    return result
