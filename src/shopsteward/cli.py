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


@app.command()
def sync(
    fixtures: Annotated[
        Path | None, typer.Option(help="Fixture dir (default source until live approved)")
    ] = None,
) -> None:
    """Pull Etsy data into the event store and rebuild projections."""
    from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild
    from shopsteward.core.sync import sync_etsy
    from shopsteward.settings import db_path

    if fixtures is None:
        typer.secho(
            "Live Etsy sync is gated on operator approval (PRD §8.4); pass --fixtures.",
            fg="red",
        )
        raise typer.Exit(1)
    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    result = sync_etsy(conn, FixtureEtsyAdapter(fixtures), user_id=1)
    rebuild(conn)
    typer.echo(f"synced: {result.model_dump()}")
