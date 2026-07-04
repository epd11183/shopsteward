"""ShopSteward CLI. UI is the primary surface; this is the scriptable path."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="ShopSteward — photography workflow tool.")


class IngestMode(StrEnum):
    hero = "hero"
    mass = "mass"


@app.command()
def serve() -> None:
    """Run the FastAPI backend + local UI."""
    import uvicorn

    uvicorn.run("shopsteward.api:create_app", factory=True, host="127.0.0.1", port=8321)


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    mode: Annotated[IngestMode, typer.Option(...)],
    preset: Annotated[str | None, typer.Option(help="Mass-mode preset family")] = None,
) -> None:
    """Folder-pointed ingestion (M2 — stub until then)."""
    typer.secho(f"ingest is M2 scope (mode={mode.value}, preset={preset})", fg="yellow")
    raise typer.Exit(code=1)
