"""Atomic folder-handoff protocol shared by all file-based interfaces.

Layout inside a protocol root:
    <root>/                inbox: writers place manifests here
    <root>/done/           consumer moves successfully processed manifests here
    <root>/failed/         consumer moves failed manifests here
    <root>/quarantine/     reader moves unparseable/mis-schemed files here

Writers create '<name>.part', write JSON, fsync, then os.replace() to
'<name>' — readers never see partial files. Names must be unique per
manifest (caller's job, e.g. embed a uuid). Every manifest carries a
'schema' field like 'shopsteward.editjob/1'; readers filter on a schema
prefix and quarantine anything else.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

_DONE = "done"
_FAILED = "failed"
_QUARANTINE = "quarantine"
_OUTCOMES = {_DONE, _FAILED}


@dataclass(frozen=True)
class Manifest:
    path: Path
    payload: dict


@dataclass(frozen=True)
class QuarantinedFile:
    original_name: str
    quarantine_path: Path
    reason: str


def ensure_layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / _DONE).mkdir(exist_ok=True)
    (root / _FAILED).mkdir(exist_ok=True)
    (root / _QUARANTINE).mkdir(exist_ok=True)


def write_manifest(root: Path, name: str, payload: dict, schema: str) -> Path:
    if name.endswith(".part"):
        raise ValueError(f"manifest name must not end with '.part': {name!r}")
    if "/" in name or "\\" in name or os.sep in name:
        raise ValueError(f"manifest name must not contain a path separator: {name!r}")

    ensure_layout(root)
    final_path = root / name
    part_path = root / f"{name}.part"
    full_payload = {"schema": schema, **payload}

    with part_path.open("w", encoding="utf-8") as f:
        json.dump(full_payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())

    os.replace(part_path, final_path)
    return final_path


def _quarantine(root: Path, path: Path, reason: str) -> QuarantinedFile:
    quarantine_dir = root / _QUARANTINE
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    dest = quarantine_dir / path.name
    if dest.exists():
        stem = path.stem
        suffix = path.suffix
        counter = 1
        while True:
            candidate = quarantine_dir / f"{stem}-{counter}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
            counter += 1

    os.replace(path, dest)
    return QuarantinedFile(original_name=path.name, quarantine_path=dest, reason=reason)


def read_manifests(root: Path, schema_prefix: str) -> tuple[list[Manifest], list[QuarantinedFile]]:
    ensure_layout(root)
    manifests: list[Manifest] = []
    quarantined: list[QuarantinedFile] = []

    for path in sorted(root.glob("*.json")):
        if not path.is_file() or path.name.endswith(".part"):
            continue

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            quarantined.append(_quarantine(root, path, reason=f"invalid JSON: {exc}"))
            continue

        if not isinstance(payload, dict):
            quarantined.append(_quarantine(root, path, reason="payload is not a JSON object"))
            continue

        schema = payload.get("schema")
        if not isinstance(schema, str) or not schema.startswith(schema_prefix):
            quarantined.append(
                _quarantine(root, path, reason=f"schema {schema!r} missing or unmatched prefix")
            )
            continue

        manifests.append(Manifest(path=path, payload=payload))

    manifests.sort(key=lambda m: m.path.name)
    return manifests, quarantined


def complete(manifest_path: Path, outcome: str, result_payload: dict, result_schema: str) -> Path:
    if outcome not in _OUTCOMES:
        raise ValueError(f"outcome must be one of {_OUTCOMES!r}, got {outcome!r}")

    root = manifest_path.parent
    outcome_dir = root / outcome
    outcome_dir.mkdir(parents=True, exist_ok=True)

    moved_path = outcome_dir / manifest_path.name
    os.replace(manifest_path, moved_path)

    result_name = f"{moved_path.stem}.result.json"
    return write_manifest(outcome_dir, result_name, result_payload, result_schema)


def read_results(root: Path, schema_prefix: str) -> list[Manifest]:
    results: list[Manifest] = []

    for outcome in (_DONE, _FAILED):
        outcome_dir = root / outcome
        if not outcome_dir.exists():
            continue

        for path in sorted(outcome_dir.glob("*.result.json")):
            if not path.is_file():
                continue

            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                _quarantine(root, path, reason=f"invalid JSON: {exc}")
                continue

            if not isinstance(payload, dict):
                _quarantine(root, path, reason="payload is not a JSON object")
                continue

            schema = payload.get("schema")
            if not isinstance(schema, str) or not schema.startswith(schema_prefix):
                _quarantine(root, path, reason=f"schema {schema!r} missing or unmatched prefix")
                continue

            results.append(Manifest(path=path, payload=payload))

    results.sort(key=lambda m: m.path.name)
    return results
