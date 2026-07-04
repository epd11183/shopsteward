"""Folder-based LightroomBridge implementation, built on core.folderproto.

Layout under `root` (default `data/bridge`, see settings.bridge_dir): jobs are
written to `<root>/jobs/`; outcomes are read from `<root>/jobs/done/` and
`<root>/jobs/failed/`. See docs/designs/2026-07-03-m2-editing-module.md §3.
"""

from pathlib import Path

from shopsteward.adapters.lightroom.interface import JOB_SCHEMA, RESULT_SCHEMA
from shopsteward.core.folderproto import read_results, write_manifest
from shopsteward.editing.models import EditJobSpec


def _slashed(path: str) -> str:
    return path.replace("\\", "/")


class FolderBridge:
    """Hands edit jobs to the Lua plugin via `<root>/jobs/*.json`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def dispatch(self, job: EditJobSpec) -> str:
        payload = job.model_dump()
        for photo in payload["photos"]:
            photo["raw_path"] = _slashed(photo["raw_path"])
        if payload.get("export"):
            payload["export"]["output_folder"] = _slashed(payload["export"]["output_folder"])

        name = f"edit_{job.job_id}.json"
        write_manifest(self.root / "jobs", name, payload, JOB_SCHEMA)
        return name

    def poll_results(self) -> list[dict]:
        results = read_results(self.root / "jobs", RESULT_SCHEMA)
        return [m.payload for m in results]
