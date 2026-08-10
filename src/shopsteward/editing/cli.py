"""`shopsteward edit` sub-app: preset browsing/seeding + bridge status."""

from pathlib import Path
from typing import Annotated

import typer

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.editing import presets
from shopsteward.editing.config import PRESET_FAMILIES_DIR, load_correction_knobs
from shopsteward.editing.edit import run_edit
from shopsteward.editing.outcomes import scan_outcomes
from shopsteward.editing.projections import rebuild_editing
from shopsteward.editing.rawdecode import RawpyDecoder
from shopsteward.settings import DEFAULT_USER_ID, bridge_dir, db_path

edit_app = typer.Typer(no_args_is_help=True, help="Editing module: presets + bridge status.")
presets_app = typer.Typer(no_args_is_help=True, help="Preset family management.")
edit_app.add_typer(presets_app, name="presets")


@presets_app.command("list")
def presets_list() -> None:
    """List seeded preset families."""
    conn = connect(db_path())
    try:
        migrate(conn)
        presets.seed(conn, DEFAULT_USER_ID, PRESET_FAMILIES_DIR)
        for family in presets.list_families(conn, DEFAULT_USER_ID):
            typer.echo(f"{family.name}\t{family.description}\t{len(family.settings)} settings")
    finally:
        conn.close()


@presets_app.command("show")
def presets_show(name: Annotated[str, typer.Argument()]) -> None:
    """Show the resolved develop settings for a preset family."""
    conn = connect(db_path())
    try:
        migrate(conn)
        presets.seed(conn, DEFAULT_USER_ID, PRESET_FAMILIES_DIR)
        family = presets.get_family(conn, DEFAULT_USER_ID, name)
        typer.echo(f"{family.name}: {family.description}")
        for key, value in family.settings.items():
            typer.echo(f"  {key} = {value}")
    finally:
        conn.close()


@presets_app.command("seed")
def presets_seed() -> None:
    """Seed preset families from config/defaults/preset_families/*.json."""
    conn = connect(db_path())
    try:
        migrate(conn)
        count = presets.seed(conn, DEFAULT_USER_ID, PRESET_FAMILIES_DIR)
        typer.echo(f"seeded {count} preset families")
    finally:
        conn.close()


@edit_app.command("status")
def status() -> None:
    """Scan the bridge for new outcomes and print job/photo status."""
    conn = connect(db_path())
    try:
        migrate(conn)
        bridge = FolderBridge(bridge_dir())
        new_events = scan_outcomes(conn, DEFAULT_USER_ID, bridge)
        rebuild_editing(conn)

        typer.echo(f"new outcome events: {new_events}")

        typer.echo("ingest jobs:")
        for row in conn.execute(
            "SELECT * FROM proj_ingest_jobs WHERE user_id=?", (DEFAULT_USER_ID,)
        ).fetchall():
            typer.echo(f"  {row['ingest_job_id']}  {row['status']}  paired={row['paired']}")

        typer.echo("edit jobs:")
        for row in conn.execute(
            "SELECT * FROM proj_edit_jobs WHERE user_id=?", (DEFAULT_USER_ID,)
        ).fetchall():
            typer.echo(f"  {row['edit_job_id']}  {row['status']}  photos={row['photo_count']}")

        typer.echo("photos by status:")
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM proj_photos WHERE user_id=? GROUP BY status",
            (DEFAULT_USER_ID,),
        ).fetchall():
            typer.echo(f"  {row['status']}: {row['n']}")
    finally:
        conn.close()


def _default_decoder():
    return RawpyDecoder()


def _default_look_adapter():
    # Offline default. Live LLM generation is gated (flag+env+key) — out of scope here.
    return FixtureLookAdapter()


@edit_app.command("run")
def run(
    path: Annotated[str, typer.Argument(help="Folder of RAW files to edit")],
    look: Annotated[str, typer.Option(help="Look name or free-text description")],
    regenerate: Annotated[
        bool, typer.Option(help="Force LLM regeneration of a described look")
    ] = False,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing .xmp sidecars")] = False,
    batch_lock: Annotated[bool, typer.Option(help="Average correction across the batch")] = False,
    model: Annotated[str, typer.Option(help="LLM model id for described looks")] = "fixture",
) -> None:
    """Decode each RAW, compute correction + look, write an XMP sidecar."""
    conn = connect(db_path())
    try:
        migrate(conn)
        report = run_edit(
            conn, DEFAULT_USER_ID, Path(path), look,
            decoder=_default_decoder(), look_adapter=_default_look_adapter(),
            model=model, knobs=load_correction_knobs(),
            regenerate=regenerate, overwrite=overwrite, batch_lock=batch_lock,
        )
        typer.echo(
            f"look={report.look} processed={report.processed} written={report.written} "
            f"skipped_existing={report.skipped_existing} failed={report.failed}"
        )
    finally:
        conn.close()
