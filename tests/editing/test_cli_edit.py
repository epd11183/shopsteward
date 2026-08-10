from pathlib import Path

from typer.testing import CliRunner

from shopsteward.editing.cli import edit_app

runner = CliRunner()


def test_edit_run_reports_written(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    (tmp_path / "IMG_1.CR3").write_bytes(b"stub")

    import numpy as np

    from shopsteward.editing import cli as cli_mod
    from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

    fake = FakeRawDecoder(
        {str(tmp_path / "IMG_1.CR3"): DecodedImage(rgb=np.full((8, 8, 3), 0.2, np.float32))}
    )
    monkeypatch.setattr(cli_mod, "_default_decoder", lambda: fake)

    result = runner.invoke(edit_app, ["run", str(tmp_path), "--look", "bright-and-true"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output
    assert (tmp_path / "IMG_1.xmp").exists()
