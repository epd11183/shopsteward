import json
from pathlib import Path

import pytest

from shopsteward.core.folderproto import (
    Manifest,
    QuarantinedFile,
    complete,
    ensure_layout,
    read_manifests,
    read_results,
    write_manifest,
)

SCHEMA = "shopsteward.editjob/1"
PREFIX = "shopsteward.editjob/"
RESULT_SCHEMA = "shopsteward.editresult/1"
RESULT_PREFIX = "shopsteward.editresult/"


def test_write_manifest_produces_final_file_no_part_left(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    path = write_manifest(root, "job_1.json", {"job_id": "1"}, SCHEMA)

    assert path == root / "job_1.json"
    assert path.exists()
    assert not (root / "job_1.json.part").exists()

    payload = json.loads(path.read_text())
    assert payload == {"schema": SCHEMA, "job_id": "1"}


def test_write_manifest_rejects_bad_names(tmp_path: Path) -> None:
    root = tmp_path / "jobs"

    with pytest.raises(ValueError):
        write_manifest(root, "job_1.json.part", {}, SCHEMA)

    with pytest.raises(ValueError):
        write_manifest(root, "sub/job_1.json", {}, SCHEMA)

    with pytest.raises(ValueError):
        write_manifest(root, "sub\\job_1.json", {}, SCHEMA)


def test_read_manifests_skips_part_files(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    ensure_layout(root)
    write_manifest(root, "job_1.json", {"job_id": "1"}, SCHEMA)
    (root / "job_2.json.part").write_text(json.dumps({"schema": SCHEMA, "job_id": "2"}))

    manifests, quarantined = read_manifests(root, PREFIX)

    assert [m.path.name for m in manifests] == ["job_1.json"]
    assert quarantined == []


def test_malformed_json_is_quarantined_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    ensure_layout(root)
    (root / "bad.json").write_text("{not valid json")

    manifests, quarantined = read_manifests(root, PREFIX)

    assert manifests == []
    assert len(quarantined) == 1
    q = quarantined[0]
    assert isinstance(q, QuarantinedFile)
    assert q.original_name == "bad.json"
    assert q.quarantine_path == root / "quarantine" / "bad.json"
    assert q.quarantine_path.exists()
    assert not (root / "bad.json").exists()
    assert q.reason


def test_malformed_json_same_name_twice_gets_counter_suffix(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    ensure_layout(root)
    (root / "bad.json").write_text("{not valid json")
    read_manifests(root, PREFIX)

    (root / "bad.json").write_text("{also not valid")
    manifests, quarantined = read_manifests(root, PREFIX)

    assert manifests == []
    assert len(quarantined) == 1
    q = quarantined[0]
    assert q.quarantine_path != root / "quarantine" / "bad.json"
    assert q.quarantine_path.parent == root / "quarantine"
    assert q.quarantine_path.exists()
    assert (root / "quarantine" / "bad.json").exists()


def test_wrong_schema_quarantined_matching_prefix_returned(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    ensure_layout(root)
    write_manifest(root, "good.json", {"job_id": "1"}, "shopsteward.editjob/1")
    write_manifest(root, "wrong.json", {"job_id": "2"}, "shopsteward.other/1")
    (root / "no_schema.json").write_text(json.dumps({"job_id": "3"}))

    manifests, quarantined = read_manifests(root, PREFIX)

    assert [m.path.name for m in manifests] == ["good.json"]
    quarantined_names = {q.original_name for q in quarantined}
    assert quarantined_names == {"wrong.json", "no_schema.json"}


def test_complete_moves_to_done_and_writes_result(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manifest_path = write_manifest(root, "job_1.json", {"job_id": "1"}, SCHEMA)

    result_path = complete(
        manifest_path, "done", {"job_id": "1", "status": "completed"}, RESULT_SCHEMA
    )

    assert not manifest_path.exists()
    assert (root / "done" / "job_1.json").exists()
    assert result_path == root / "done" / "job_1.result.json"
    assert result_path.exists()
    payload = json.loads(result_path.read_text())
    assert payload == {"schema": RESULT_SCHEMA, "job_id": "1", "status": "completed"}


def test_complete_failed_moves_to_failed_and_writes_result(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manifest_path = write_manifest(root, "job_2.json", {"job_id": "2"}, SCHEMA)

    result_path = complete(
        manifest_path, "failed", {"job_id": "2", "error": {"code": "boom"}}, RESULT_SCHEMA
    )

    assert not manifest_path.exists()
    assert (root / "failed" / "job_2.json").exists()
    assert result_path == root / "failed" / "job_2.result.json"
    assert result_path.exists()


def test_complete_bad_outcome_raises(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    manifest_path = write_manifest(root, "job_3.json", {"job_id": "3"}, SCHEMA)

    with pytest.raises(ValueError):
        complete(manifest_path, "bogus", {}, RESULT_SCHEMA)


def test_read_results_returns_from_done_and_failed_ignores_originals(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    m1 = write_manifest(root, "job_1.json", {"job_id": "1"}, SCHEMA)
    m2 = write_manifest(root, "job_2.json", {"job_id": "2"}, SCHEMA)
    complete(m1, "done", {"job_id": "1", "status": "completed"}, RESULT_SCHEMA)
    complete(m2, "failed", {"job_id": "2", "status": "failed"}, RESULT_SCHEMA)

    results = read_results(root, RESULT_PREFIX)

    assert [r.path.name for r in results] == ["job_1.result.json", "job_2.result.json"]
    assert all(isinstance(r, Manifest) for r in results)
    job_ids = {r.payload["job_id"] for r in results}
    assert job_ids == {"1", "2"}


def test_full_producer_consumer_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    write_manifest(root, "job_1.json", {"job_id": "1", "photos": ["a"]}, SCHEMA)

    manifests, quarantined = read_manifests(root, PREFIX)
    assert quarantined == []
    assert len(manifests) == 1

    complete(
        manifests[0].path,
        "done",
        {"job_id": "1", "status": "completed", "applied": 1},
        RESULT_SCHEMA,
    )

    results = read_results(root, RESULT_PREFIX)
    assert len(results) == 1
    assert results[0].payload["job_id"] == "1"
    assert results[0].payload["status"] == "completed"
