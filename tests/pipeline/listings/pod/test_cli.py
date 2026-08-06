from PIL import Image
from typer.testing import CliRunner

from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1
runner = CliRunner()


def _seed_landing_file(db_path, tmp_path) -> None:
    conn = connect(db_path)
    migrate(conn)
    path = tmp_path / "hero.jpg"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(path, "JPEG")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": "f1",
                "path": str(path),
                "base_name": "hero",
                "format": "JPEG",
                "width": 6000,
                "height": 4000,
                "color_space": "sRGB",
                "photo_id": "p1",
            },
        ),
    )
    rebuild_pipeline(conn)
    conn.close()


def test_pod_build_dry_run_prints_and_appends_nothing(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    _seed_landing_file(db, tmp_path)

    result = runner.invoke(app, ["pod", "build", "--dry-run"])

    assert result.exit_code == 0
    assert "KEEP acrylic/acrylic_16x24" in result.output
    assert "price=149.00" in result.output

    conn = connect(db)
    migrate(conn)
    n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE type LIKE 'listingdraft.%'").fetchone()
    assert n["n"] == 0  # dry-run appended nothing
    conn.close()


def test_pod_build_appends_and_reports(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    _seed_landing_file(db, tmp_path)

    result = runner.invoke(app, ["pod", "build"])

    assert result.exit_code == 0
    assert "drafts_built" in result.output

    conn = connect(db)
    migrate(conn)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='listingdraft.print_file_hosted'"
    ).fetchone()
    assert n["n"] == 3  # acrylic, poster, canvas
    conn.close()


def test_pod_build_live_printfile_without_gate_refuses(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_PRINTFILE", raising=False)
    _seed_landing_file(db, tmp_path)

    result = runner.invoke(app, ["pod", "build", "--live-printfile"])

    assert result.exit_code == 1
    assert "gated on operator approval" in result.output
