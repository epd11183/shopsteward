"""Python reimplementation of the Lua queue processor's consumer contract, for
tests. Note: `render_name` mirrors `JobFile.render_name` in the Lua plugin —
keep the two in sync if the naming-template grammar changes. Malformed job
files land in `failed/` with a result manifest, exactly like the Lua consumer
(never quarantined — quarantine is reader-side only)."""

import json
import os
from pathlib import Path

from shopsteward.adapters.lightroom.interface import JOB_SCHEMA, RESULT_SCHEMA
from shopsteward.core.folderproto import complete, write_manifest

_FINISHED_AT = "2026-01-01T00:00:00Z"


def render_name(template: str, *, event: str, date: str, seq: int, base: str) -> str:
    """Pure rename function: `{event}`, `{date}`, `{seq:04}`-style padding, `{base}`."""
    return template.format(event=event, date=date, seq=seq, base=base)


def _parse_job(path: Path) -> tuple[dict | None, str]:
    """Parse + schema-check a job file. Returns (payload, "") or (None, reason)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "job payload is not a JSON object"
    if payload.get("schema") != JOB_SCHEMA:
        return None, f"schema is not {JOB_SCHEMA} (got {payload.get('schema')!r})"
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return None, "job_id must be a non-empty string"
    return payload, ""


class FakeBridge:
    """Stands in for the Lightroom-side queue processor in tests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def consume_all(self) -> None:
        jobs_root = self.root / "jobs"
        if not jobs_root.is_dir():
            return

        for path in sorted(jobs_root.glob("*.json")):
            if not path.is_file() or path.name.endswith((".part", ".result.json")):
                continue
            payload, reason = _parse_job(path)
            if payload is None:
                self._fail_malformed(jobs_root, path, reason)
            else:
                self._process(path, payload)

    def _fail_malformed(self, jobs_root: Path, path: Path, reason: str) -> None:
        """Mirror the Lua consumer: malformed job -> failed/ + result manifest
        whose job_id is the filename stem with a leading 'edit_' stripped."""
        failed_dir = jobs_root / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        os.replace(path, failed_dir / path.name)
        write_manifest(
            failed_dir,
            f"{path.stem}.result.json",
            {
                "job_id": path.stem.removeprefix("edit_"),
                "status": "failed",
                "applied": 0,
                "skipped": [],
                "exported": [],
                "error": {"code": "malformed", "message": reason},
                "file_name": path.name,
                "finished_at": _FINISHED_AT,
            },
            RESULT_SCHEMA,
        )

    def _process(self, path: Path, payload: dict) -> None:
        job_id = payload["job_id"]

        if payload.get("_force_fail"):
            complete(
                path,
                "failed",
                {
                    "job_id": job_id,
                    "status": "failed",
                    "applied": 0,
                    "skipped": [],
                    "exported": [],
                    "error": {"code": "apply_error", "message": "forced"},
                    "finished_at": _FINISHED_AT,
                },
                RESULT_SCHEMA,
            )
            return

        photos = payload["photos"]
        export = payload.get("export")
        exported: list[str] = []

        if export is not None:
            output_folder = Path(export["output_folder"])
            output_folder.mkdir(parents=True, exist_ok=True)
            for seq, photo in enumerate(photos, start=1):
                rendered = render_name(
                    export["naming_template"],
                    event=export["event"],
                    date=_FINISHED_AT[:10],
                    seq=seq,
                    base=photo["base_name"],
                )
                filename = f"{rendered}.jpg"
                (output_folder / filename).touch()
                exported.append(filename)

        complete(
            path,
            "done",
            {
                "job_id": job_id,
                "status": "completed",
                "applied": len(photos),
                "skipped": [],
                "exported": exported,
                "finished_at": _FINISHED_AT,
            },
            RESULT_SCHEMA,
        )
