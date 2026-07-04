#!/usr/bin/env python3
"""Generate the six canned manual-test job files for the ShopSteward queue
processor. Standalone: stdlib only, no shopsteward imports — runs anywhere.

Usage:
    python make_test_jobs.py --bridge-dir C:/path/to/bridge

Writes into <bridge-dir>/jobs/ using the same atomic '.part' + rename
convention as the real dispatcher. Expected outcomes are printed per file;
the full checklist lives in TESTING.md.

Before starting the queue processor, edit the EDIT_ME raw_path values in
jobs 1, 2 and 6 to point at real files on your machine (small JPEGs are
fine — Lightroom imports them like any photo).
"""

import argparse
import json
import os
import uuid
from pathlib import Path

SCHEMA = "shopsteward.editjob/1"

DEV_SETTINGS = {"Contrast2012": 10, "Vibrance": 15}


def write_atomic(jobs_dir: Path, name: str, text: str) -> None:
    part = jobs_dir / f"{name}.part"
    part.write_text(text, encoding="utf-8")
    os.replace(part, jobs_dir / name)


def job(job_id: str, photos: list[dict], collection: str, export: dict | None) -> dict:
    return {
        "schema": SCHEMA,
        "job_id": job_id,
        "user_id": 1,
        "mode": "mass",
        "preset_family": "test-family",
        "develop_settings": DEV_SETTINGS,
        "photos": photos,
        "collection": collection,
        "import_missing": True,
        "export": export,
    }


def export_block(out_dir: str, event: str) -> dict:
    return {
        "output_folder": out_dir,
        "naming_template": "{event}-{seq:04}",
        "event": event,
        "jpeg_quality": 92,
        "color_space": "sRGB",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", required=True, help="bridge root (jobs/ created inside)")
    args = parser.parse_args()

    bridge = Path(args.bridge_dir)
    jobs_dir = bridge / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    out_root = (bridge / "test_exports").as_posix()

    expectations: list[tuple[str, str]] = []

    # 1. Valid mass job — edit the raw_path placeholders before running.
    name = f"edit_test1_valid_{uuid.uuid4().hex[:8]}.json"
    write_atomic(
        jobs_dir,
        name,
        json.dumps(
            job(
                "test1-valid",
                [
                    {"base_name": "TEST_A", "raw_path": "C:/EDIT_ME/TEST_A.jpg"},
                    {"base_name": "TEST_B", "raw_path": "C:/EDIT_ME/TEST_B.jpg"},
                ],
                "ShopSteward — test-valid",
                export_block(f"{out_root}/valid", "testev"),
            ),
            indent=2,
        ),
    )
    expectations.append(
        (name, "done/ — applied=2, exports testev-0001.jpg + testev-0002.jpg, "
               "collection 'ShopSteward — test-valid' created")
    )

    # 2. Valid mass job with one nonexistent raw_path — skip case.
    name = f"edit_test2_skip_{uuid.uuid4().hex[:8]}.json"
    write_atomic(
        jobs_dir,
        name,
        json.dumps(
            job(
                "test2-skip",
                [
                    {"base_name": "TEST_A", "raw_path": "C:/EDIT_ME/TEST_A.jpg"},
                    {"base_name": "GHOST", "raw_path": "C:/does/not/exist/GHOST.CR3"},
                ],
                "ShopSteward — test-skip",
                export_block(f"{out_root}/skip", "skipev"),
            ),
            indent=2,
        ),
    )
    expectations.append(
        (name, "done/ — applied=1, skipped=[{GHOST, not_in_catalog}], one export skipev-0001.jpg")
    )

    # 3. Malformed JSON.
    name = f"edit_test3_malformed_{uuid.uuid4().hex[:8]}.json"
    write_atomic(jobs_dir, name, '{ "schema": "shopsteward.editjob/1", "job_id": ')
    expectations.append((name, "failed/ — result error.code=malformed (JSON parse error)"))

    # 4. Wrong schema tag.
    name = f"edit_test4_wrongschema_{uuid.uuid4().hex[:8]}.json"
    bad = job(
        "test4-wrongschema",
        [{"base_name": "TEST_A", "raw_path": "C:/EDIT_ME/TEST_A.jpg"}],
        "ShopSteward — test-wrongschema",
        export_block(f"{out_root}/wrongschema", "wrongev"),
    )
    bad["schema"] = "shopsteward.other/9"
    write_atomic(jobs_dir, name, json.dumps(bad, indent=2))
    expectations.append((name, "failed/ — result error.code=malformed (schema mismatch)"))

    # 5. Empty photos array — validation error. (The plugin creates missing
    # output folders itself, so a nonexistent output dir is not an error case.)
    name = f"edit_test5_nophotos_{uuid.uuid4().hex[:8]}.json"
    write_atomic(
        jobs_dir,
        name,
        json.dumps(
            job(
                "test5-nophotos",
                [],
                "ShopSteward — test-nophotos",
                export_block(f"{out_root}/nophotos", "emptyev"),
            ),
            indent=2,
        ),
    )
    expectations.append((name, "failed/ — result error.code=malformed (photos must be non-empty)"))

    # 6. Unicode base_name and event.
    name = f"edit_test6_unicode_{uuid.uuid4().hex[:8]}.json"
    write_atomic(
        jobs_dir,
        name,
        json.dumps(
            job(
                "test6-unicode",
                [{"base_name": "café_żółć", "raw_path": "C:/EDIT_ME/TEST_A.jpg"}],
                "ShopSteward — tëst-únicode",
                export_block(f"{out_root}/unicode", "smith–wedding café"),
            ),
            indent=2,
            ensure_ascii=False,
        ),
    )
    expectations.append(
        (name, "done/ — applied=1, export 'smith–wedding café-0001.jpg', unicode intact in result")
    )

    print(f"Wrote 6 test jobs to {jobs_dir}\n")
    print("Edit the C:/EDIT_ME/... raw_path values in jobs 1, 2 and 6 first.\n")
    print("Expected outcomes once the queue processor runs:")
    for fname, expected in expectations:
        print(f"  {fname}\n      -> {expected}")


if __name__ == "__main__":
    main()
