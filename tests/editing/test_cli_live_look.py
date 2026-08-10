from pathlib import Path

from typer.testing import CliRunner

from shopsteward.editing.cli import edit_app

runner = CliRunner()


def test_live_look_refused_when_gate_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_LOOK", raising=False)
    (tmp_path / "IMG.CR3").write_bytes(b"x")
    result = runner.invoke(
        edit_app, ["run", str(tmp_path), "--look", "brand new look", "--live-look"]
    )
    assert result.exit_code != 0
    assert "SHOPSTEWARD_LIVE_LOOK" in result.output


def test_named_look_offline_still_works(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    (tmp_path / "IMG.CR3").write_bytes(b"x")
    import numpy as np

    from shopsteward.editing import cli as cli_mod
    from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

    fake = FakeRawDecoder(
        {str(tmp_path / "IMG.CR3"): DecodedImage(rgb=np.full((8, 8, 3), 0.2, np.float32))}
    )
    monkeypatch.setattr(cli_mod, "_default_decoder", lambda: fake)
    result = runner.invoke(edit_app, ["run", str(tmp_path), "--look", "bright-and-true"])
    assert result.exit_code == 0, result.output
    assert "written=1" in result.output


def test_live_look_refused_when_model_has_no_pricing(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SHOPSTEWARD_LIVE_LOOK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    (tmp_path / "IMG.CR3").write_bytes(b"x")
    from shopsteward.editing import cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_look_llm",
                        lambda: {"provider": "openrouter", "model": "no/pricing", "pricing": {},
                                 "monthly_soft_cap_usd": 5.0, "structured_output": False})
    from typer.testing import CliRunner
    result = CliRunner().invoke(
        cli_mod.edit_app, ["run", str(tmp_path), "--look", "x", "--live-look"]
    )
    assert result.exit_code != 0
    assert "pricing" in result.output
