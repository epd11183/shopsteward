"""ShopSteward CLI. UI is the primary surface; this is the scriptable path."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from shopsteward.editing.cli import edit_app
from shopsteward.pipeline.cli import pipeline_app, score_app

app = typer.Typer(no_args_is_help=True, help="ShopSteward — photography workflow tool.")
app.add_typer(edit_app, name="edit")
app.add_typer(score_app, name="score")
app.add_typer(pipeline_app, name="pipeline")


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
    event: Annotated[str | None, typer.Option("--event", help="Event name")] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Mass-mode export folder")] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the mass-mode confirmation prompt")
    ] = False,
) -> None:
    """Folder-pointed ingestion: pair RAW+JPEG, hash, record.

    Hero mode records photos as `awaiting_scoring` (M3 picks them up). Mass
    mode also resolves a preset family and dispatches an edit job to the
    Lightroom bridge.
    """
    from shopsteward.adapters.lightroom.bridge import FolderBridge
    from shopsteward.core.db import connect, migrate
    from shopsteward.editing import presets
    from shopsteward.editing.config import PRESET_FAMILIES_DIR, load_editing_defaults
    from shopsteward.editing.dispatch import dispatch_edit_job
    from shopsteward.editing.ingest import ingest_folder
    from shopsteward.editing.outcomes import scan_outcomes
    from shopsteward.editing.projections import rebuild_editing
    from shopsteward.settings import DEFAULT_USER_ID, bridge_dir, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        presets.seed(conn, DEFAULT_USER_ID, PRESET_FAMILIES_DIR)

        if mode == IngestMode.hero:
            report = ingest_folder(
                conn,
                DEFAULT_USER_ID,
                path,
                mode.value,
                preset_family=preset,
                event_name=event,
                output_folder=str(out) if out else None,
            )
            rebuild_editing(conn)
            typer.echo(f"ingested: {report.model_dump()}")
            typer.echo("Photos await scoring (M3).")
            return

        # Mass mode: resolve and validate the preset family BEFORE ingesting,
        # so a typo'd --preset causes no side effects.
        family_name = preset
        if family_name is None:
            families = presets.list_families(conn, DEFAULT_USER_ID)
            typer.echo(f"Preset families: {', '.join(f.name for f in families)}")
            family_name = typer.prompt("Preset family")
        try:
            presets.get_family(conn, DEFAULT_USER_ID, family_name)
        except KeyError:
            names = ", ".join(f.name for f in presets.list_families(conn, DEFAULT_USER_ID))
            typer.secho(
                f"Unknown preset family '{family_name}'. Available: {names or '(none seeded)'}",
                fg="red",
            )
            raise typer.Exit(code=1) from None

        editing_defaults = load_editing_defaults()
        output_folder = (
            str(out)
            if out
            else str(Path(editing_defaults["event_output_root"]) / (event or "untitled"))
        )

        report = ingest_folder(
            conn,
            DEFAULT_USER_ID,
            path,
            mode.value,
            preset_family=family_name,
            event_name=event,
            output_folder=output_folder,
        )
        typer.echo(f"ingested: {report.model_dump()}")

        if not report.photo_ids:
            rebuild_editing(conn)
            typer.echo("no new photos to dispatch")
            return

        if not yes and not typer.confirm(
            f"Apply '{family_name}' to {len(report.photo_ids)} NEW photos -> {output_folder}?"
        ):
            rebuild_editing(conn)
            typer.echo(
                "Not dispatched. Photos remain queued_for_edit; "
                "re-run ingest later to dispatch (ingestion is idempotent)."
            )
            return

        bridge = FolderBridge(bridge_dir())
        job = dispatch_edit_job(
            conn,
            DEFAULT_USER_ID,
            bridge,
            photo_ids=report.photo_ids,
            preset_family=family_name,
            mode=mode.value,
            event_name=event,
            output_folder=output_folder,
            editing_defaults=editing_defaults,
        )
        scan_outcomes(conn, DEFAULT_USER_ID, bridge)
        rebuild_editing(conn)

        typer.echo(f"dispatched edit job {job.job_id}")
    finally:
        conn.close()


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
    from shopsteward.settings import DEFAULT_USER_ID, db_path

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
    result = sync_etsy(conn, FixtureEtsyAdapter(fixtures), user_id=DEFAULT_USER_ID)
    rebuild(conn)
    typer.echo(f"synced: {result.model_dump()}")
