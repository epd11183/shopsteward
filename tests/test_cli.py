from typer.testing import CliRunner

from shopsteward.cli import app

runner = CliRunner()


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "ingest" in result.output


def test_ingest_requires_mode():
    result = runner.invoke(app, ["ingest", "some/path"])
    assert result.exit_code != 0  # --mode is required
