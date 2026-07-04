"""Build an EditJobSpec (develop settings inlined), hand it to the Lightroom
bridge, and record the dispatch as an event."""

import sqlite3
import uuid
from pathlib import Path

from shopsteward.adapters.lightroom.interface import LightroomBridge
from shopsteward.core.events import Event, append, read_all
from shopsteward.editing import presets
from shopsteward.editing.models import EditJobSpec, ExportSpec


def _photo_records(conn: sqlite3.Connection, user_id: int, photo_ids: list[str]) -> dict[str, dict]:
    wanted = set(photo_ids)
    records: dict[str, dict] = {}
    for e in read_all(conn, "photo.ingested"):
        if e.user_id != user_id:
            continue
        photo_id = e.payload["photo_id"]
        if photo_id in wanted:
            records[photo_id] = e.payload
    return records


def dispatch_edit_job(
    conn: sqlite3.Connection,
    user_id: int,
    bridge: LightroomBridge,
    *,
    photo_ids: list[str],
    preset_family: str,
    mode: str,
    event_name: str | None,
    output_folder: str | None,
    editing_defaults: dict,
) -> EditJobSpec:
    if not photo_ids:
        raise ValueError("photo_ids is empty; nothing to dispatch")
    family = presets.get_family(conn, user_id, preset_family)
    records = _photo_records(conn, user_id, photo_ids)

    photos: list[dict] = []
    ingest_job_id: str | None = None
    for photo_id in photo_ids:
        record = records.get(photo_id)
        if record is None:
            continue
        if ingest_job_id is None:
            ingest_job_id = record.get("ingest_job_id")
        photos.append({"base_name": record["base_name"], "raw_path": record["raw_path"]})

    collection = f"ShopSteward — {event_name}" if event_name else "ShopSteward — Needs Finishing"

    export: ExportSpec | None = None
    if mode != "hero":
        folder = output_folder or str(
            Path(editing_defaults["event_output_root"]) / (event_name or "untitled")
        )
        export = ExportSpec(
            # Absolute: the Lua consumer must never resolve paths relative to
            # its own working directory.
            output_folder=str(Path(folder).resolve()),
            naming_template=editing_defaults["naming_template"],
            event=event_name or "untitled",
            jpeg_quality=editing_defaults["jpeg_quality"],
        )

    job = EditJobSpec(
        job_id=uuid.uuid4().hex,
        user_id=user_id,
        mode=mode,
        preset_family=preset_family,
        develop_settings=family.settings,
        photos=photos,
        collection=collection,
        export=export,
    )

    job_file = bridge.dispatch(job)

    append(
        conn,
        Event(
            user_id=user_id,
            type="editjob.dispatched",
            payload={
                "edit_job_id": job.job_id,
                "ingest_job_id": ingest_job_id,
                "photo_ids": photo_ids,
                "preset_family": preset_family,
                "mode": mode,
                "collection": collection,
                "job_file": job_file,
                "export": export.model_dump() if export else None,
            },
        ),
    )

    return job
