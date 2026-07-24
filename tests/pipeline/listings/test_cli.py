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
        mockups_dir=tmp_path / "mockups",
    )
    conn.close()

    build_result = runner.invoke(app, ["listings", "build"])
    assert build_result.exit_code == 0
    assert "'drafts_built': 1" in build_result.output
    assert "'pushed': 1" in build_result.output

    status_result = runner.invoke(app, ["listings", "status"])
    assert status_result.exit_code == 0
    assert "Listing drafts: 1" in status_result.output
    # build folds push automatically (Fake adapter, offline default) so the
    # single draft ends this run in state=pushed, not built.
    assert "pushed: 1" in status_result.output


def test_listings_build_live_copy_without_env_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_COPY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = runner.invoke(app, ["listings", "build", "--live-copy"])
    assert result.exit_code == 1
    assert "SHOPSTEWARD_LIVE_COPY" in result.output


def test_listings_build_live_etsy_write_without_env_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tmp_path / "tokens.json"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_WRITE", raising=False)

    result = runner.invoke(app, ["listings", "build", "--live-etsy-write"])
    assert result.exit_code == 1
    assert "SHOPSTEWARD_LIVE_ETSY_WRITE" in result.output


def test_listings_push_without_env_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tmp_path / "tokens.json"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_WRITE", raising=False)

    result = runner.invoke(app, ["listings", "push", "--live-etsy-write"])
    assert result.exit_code == 1
    assert "SHOPSTEWARD_LIVE_ETSY_WRITE" in result.output


def test_listings_push_command_pushes_built_drafts(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))

    photo_path = tmp_path / "hero2.jpg"
    Image.new("RGB", (100, 100), (3, 3, 3)).save(photo_path, "JPEG")

    conn = connect(db)
    migrate(conn)
    seed_landing_file_with_mockup_set(
        conn,
        file_id="e" * 64,
        photo_id="photo-cli-2",
        path=str(photo_path),
        set_key="set-cli-2",
        intents=["single"],
        mockups_dir=tmp_path / "mockups",
    )
    conn.close()

    # `listings build` already pushes automatically; `listings push` against
    # an already-pushed draft is a no-op (idempotent, never a duplicate).
    build_result = runner.invoke(app, ["listings", "build"])
    assert build_result.exit_code == 0

    push_result = runner.invoke(app, ["listings", "push"])
    assert push_result.exit_code == 0
    assert "'pushed': 0" in push_result.output
