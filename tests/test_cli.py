from pathlib import Path

from typer.testing import CliRunner

from shopsteward.cli import app

runner = CliRunner()


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "ingest" in result.output


def test_ingest_requires_mode(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", str(tmp_path)])
    assert result.exit_code != 0  # --mode is required; path exists so that's the only error
