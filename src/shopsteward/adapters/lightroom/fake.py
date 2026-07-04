"""Python reimplementation of the Lua queue processor's consumer contract, for
tests. Note: `render_name` mirrors `JobFile.render_name` in the Lua plugin —
keep the two in sync if the naming-template grammar changes."""

from pathlib import Path

from shopsteward.adapters.lightroom.interface import JOB_SCHEMA, RESULT_SCHEMA
from shopsteward.core.folderproto import complete, read_manifests

_FINISHED_AT = "2026-01-01T00:00:00Z"


def render_name(template: str, *, event: str, date: str, seq: int, base: str) -> str:
    """Pure rename function: `{event}`, `{date}`, `{seq:04}`-style padding, `{base}`."""
    return template.format(event=event, date=date, seq=seq, base=base)


class FakeBridge:
    """Stands in for the Lightroom-side queue processor in tests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def consume_all(self) -> None:
        jobs_root = self.root / "jobs"
        manifests, _quarantined = read_manifests(jobs_root, JOB_SCHEMA)

        for manifest in manifests:
            payload = manifest.payload
            job_id = payload["job_id"]

            if payload.get("_force_fail"):
                complete(
                    manifest.path,
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
                continue

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
                manifest.path,
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
