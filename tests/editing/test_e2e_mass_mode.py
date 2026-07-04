"""E2E mass-mode flow without Lightroom, per design §7: ingest → dispatch →
FakeBridge simulates the Lua consumer → outcomes scan → projections."""

from pathlib import Path

from PIL import Image
from typer.testing import CliRunner

from shopsteward.adapters.lightroom.fake import FakeBridge
from shopsteward.cli import app
from shopsteward.core.db import connect
from shopsteward.core.events import read_all

runner = CliRunner()


def _make_jpeg(path: Path) -> None:
    Image.new("RGB", (8, 8)).save(path, "JPEG")


def _make_raw(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def test_e2e_mass_mode(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    bridge_root = tmp_path / "bridge"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.setenv("SHOPSTEWARD_BRIDGE_DIR", str(bridge_root))

    shoot = tmp_path / "shoot"
    shoot.mkdir()
    for name in ("a", "b", "c"):
        _make_raw(shoot / f"{name}.CR3", f"raw-{name}".encode())
        _make_jpeg(shoot / f"{name}.jpg")
    # "d" duplicates "a"'s raw content -> counted as a duplicate, not paired.
    _make_raw(shoot / "d.CR3", b"raw-a")
    _make_jpeg(shoot / "d.jpg")
    # orphan JPEG with no matching RAW.
    _make_jpeg(shoot / "orphan.jpg")

    out_dir = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "ingest",
            str(shoot),
            "--mode",
            "mass",
            "--preset",
            "neutral",
            "--event",
            "testev",
            "--out",
            str(out_dir),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "'paired': 3" in result.output
    assert "'duplicates': 1" in result.output
    assert "'unpaired': 1" in result.output
    assert "dispatched edit job" in result.output

    jobs_dir = bridge_root / "jobs"
    job_files = [p for p in jobs_dir.glob("*.json") if not p.name.endswith(".result.json")]
    assert len(job_files) == 1

    # A malformed job file dropped directly in the jobs inbox must be
    # quarantined, not crash the consumer.
    (jobs_dir / "junk.json").write_text("{not valid json")

    FakeBridge(bridge_root).consume_all()

    quarantine_dir = jobs_dir / "quarantine"
    assert [p.name for p in quarantine_dir.glob("*.json")] == ["junk.json"]

    status_result = runner.invoke(app, ["edit", "status"])
    assert status_result.exit_code == 0, status_result.output
    assert "new outcome events: 1" in status_result.output

    conn = connect(db)
    try:
        completed = read_all(conn, "editjob.completed")
        assert len(completed) == 1
        assert completed[0].payload["applied"] == 3

        statuses = {r["status"] for r in conn.execute("SELECT status FROM proj_photos").fetchall()}
        assert statuses == {"edited"}
    finally:
        conn.close()

    exported_names = sorted(p.name for p in out_dir.glob("*.jpg"))
    assert exported_names == ["testev-0001.jpg", "testev-0002.jpg", "testev-0003.jpg"]

    # Second status scan is idempotent — no new outcome events.
    second_status = runner.invoke(app, ["edit", "status"])
    assert "new outcome events: 0" in second_status.output


def test_e2e_hero_mode_no_dispatch(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    bridge_root = tmp_path / "bridge"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.setenv("SHOPSTEWARD_BRIDGE_DIR", str(bridge_root))

    shoot = tmp_path / "shoot"
    shoot.mkdir()
    _make_raw(shoot / "a.CR3", b"raw-a")
    _make_jpeg(shoot / "a.jpg")

    result = runner.invoke(app, ["ingest", str(shoot), "--mode", "hero"])
    assert result.exit_code == 0, result.output
    assert "await scoring" in result.output.lower()
    assert not (bridge_root / "jobs").exists()
