from PIL import Image
from typer.testing import CliRunner

from shopsteward.cli import app
from shopsteward.core.db import connect, migrate

from .helpers import seed_landing_file_with_mockup_set

runner = CliRunner()


def test_listings_status_empty(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))

    result = runner.invoke(app, ["listings", "status"])
    assert result.exit_code == 0
    assert "Listing drafts: 0" in result.output


def test_listings_build_then_status(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))

    photo_path = tmp_path / "hero.jpg"
    Image.new("RGB", (100, 100), (9, 9, 9)).save(photo_path, "JPEG")

    conn = connect(db)
    migrate(conn)
    seed_landing_file_with_mockup_set(
        conn,
        file_id="c" * 64,
        photo_id="photo-cli",
        path=str(photo_path),
        set_key="set-cli",
        intents=["single", "digital_whatyougot"],
    )
    conn.close()

    build_result = runner.invoke(app, ["listings", "build"])
    assert build_result.exit_code == 0
    assert "'drafts_built': 1" in build_result.output

    status_result = runner.invoke(app, ["listings", "status"])
    assert status_result.exit_code == 0
    assert "Listing drafts: 1" in status_result.output
    assert "built: 1" in status_result.output
