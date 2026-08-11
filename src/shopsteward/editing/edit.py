"""Mass-mode edit orchestration: resolve look (fail fast) -> ingest RAWs ->
per-photo decode/analyze/compose/write sidecar -> events -> EditReport.

Jobs report synchronously via the returned EditReport; they are not tracked
in the bridge-era proj_edit_jobs projection."""

import sqlite3
import uuid
from pathlib import Path

from shopsteward.adapters.look.interface import LookAdapter
from shopsteward.core.events import Event, append
from shopsteward.editing import looks
from shopsteward.editing.analyze import analyze_raw, average_corrections
from shopsteward.editing.ingest import RAW_SUFFIXES, ingest_folder
from shopsteward.editing.models import CorrectionSettings, EditReport
from shopsteward.editing.rawdecode import RawDecoder
from shopsteward.editing.xmp import compose, write_sidecar


def run_edit(
    conn: sqlite3.Connection,
    user_id: int,
    path: Path,
    look_arg: str,
    *,
    decoder: RawDecoder,
    look_adapter: LookAdapter,
    model: str,
    knobs: dict,
    regenerate: bool,
    overwrite: bool,
    batch_lock: bool,
    guard_knobs: dict | None = None,
    soft_cap_usd: float | None = None,
    fallback_look: str = "bright-and-true",
    month_prefix: str | None = None,
) -> EditReport:
    # 1. Resolve the look FIRST — this may hit the LLM and must fail before any
    #    sidecar is written, so a batch is never half-graded.
    looks.seed(conn, user_id, _looks_dir())
    look = looks.resolve_look(
        conn,
        user_id,
        look_arg,
        look_adapter,
        model=model,
        regenerate=regenerate,
        guard_knobs=guard_knobs,
        soft_cap_usd=soft_cap_usd,
        fallback_look=fallback_look,
        month_prefix=month_prefix,
    )

    edit_job_id = str(uuid.uuid4())
    report = EditReport(edit_job_id=edit_job_id, look=look.name)
    append(
        conn,
        Event(
            user_id=user_id,
            type="editjob.started",
            payload={
                "edit_job_id": edit_job_id,
                "path": str(path),
                "look": look.name,
                "batch_lock": batch_lock,
            },
        ),
    )

    # Ingest for event-sourced tracking (identity, exif, dedup bookkeeping);
    # the edit pass itself processes every RAW physically present in the
    # folder, not just this call's non-duplicate photo_ids — content-identical
    # fixtures (or a re-run) must not silently drop frames from the edit.
    ingest_folder(conn, user_id, path, mode="mass", require_jpeg=False)
    raw_paths = _raw_paths_in(path)

    # 2. Decode + analyze every frame (needed up-front for batch-lock averaging).
    corrections: dict[str, CorrectionSettings] = {}
    for rp in raw_paths:
        try:
            img = decoder.decode(str(rp))
            corrections[str(rp)] = analyze_raw(img, knobs)
        except Exception as exc:  # noqa: BLE001 - decode errors are per-frame, non-fatal
            report.failed += 1
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="sidecar.failed",
                    payload={"edit_job_id": edit_job_id, "raw_path": str(rp), "error": repr(exc)},
                ),
            )

    # batch_lock: consistent exposure/shadow across a burst. WB is always As Shot,
    # so nothing WB to lock.
    if batch_lock and corrections:
        locked = average_corrections(list(corrections.values()))
        corrections = {k: locked for k in corrections}

    # 3. Compose + write.
    for rp in raw_paths:
        if str(rp) not in corrections:
            continue  # decode failed above
        report.processed += 1
        xmp = compose(corrections[str(rp)], look)
        try:
            wrote = write_sidecar(rp, xmp, overwrite=overwrite)
        except OSError as exc:
            report.failed += 1
            report.processed -= 1  # not a completed process attempt
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="sidecar.failed",
                    payload={"edit_job_id": edit_job_id, "raw_path": str(rp), "error": repr(exc)},
                ),
            )
            continue
        if wrote:
            report.written += 1
            report.sidecar_paths.append(str(rp.with_suffix(".xmp")))
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="sidecar.written",
                    payload={"edit_job_id": edit_job_id, "raw_path": str(rp)},
                ),
            )
        else:
            report.skipped_existing += 1

    append(
        conn,
        Event(
            user_id=user_id,
            type="editjob.completed",
            payload={
                "edit_job_id": edit_job_id,
                "processed": report.processed,
                "written": report.written,
                "skipped_existing": report.skipped_existing,
                "failed": report.failed,
            },
        ),
    )
    return report


def _looks_dir() -> Path:
    from shopsteward.editing.config import LOOKS_DIR

    return LOOKS_DIR


def _raw_paths_in(path: Path) -> list[Path]:
    folder = Path(path)
    if not folder.is_dir():
        return []
    return sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in RAW_SUFFIXES)
