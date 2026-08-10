from pathlib import Path

from shopsteward.core.db import connect, migrate
from shopsteward.editing.ingest import ingest_folder

USER = 1


def test_raw_only_ingested_when_jpeg_not_required(tmp_path: Path):
    (tmp_path / "IMG_1.CR3").write_bytes(b"raw-bytes-1")
    (tmp_path / "IMG_2.CR3").write_bytes(b"raw-bytes-2")
    conn = connect(":memory:")
    migrate(conn)
    report = ingest_folder(conn, USER, tmp_path, mode="mass", require_jpeg=False)
    assert report.paired == 2
    assert report.unpaired == 0


def test_raw_only_still_unpaired_when_jpeg_required(tmp_path: Path):
    (tmp_path / "IMG_1.CR3").write_bytes(b"raw-bytes-1")
    conn = connect(":memory:")
    migrate(conn)
    report = ingest_folder(conn, USER, tmp_path, mode="mass", require_jpeg=True)
    assert report.paired == 0
    assert report.unpaired == 1
