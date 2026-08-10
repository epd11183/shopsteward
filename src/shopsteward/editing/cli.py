"""`shopsteward edit` sub-app: preset browsing/seeding + bridge status."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from shopsteward.adapters.lightroom.bridge import FolderBridge
from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.adapters.look.interface import LookParseError
from shopsteward.adapters.look.openrouter import OpenRouterLookAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.editing import presets
from shopsteward.editing.config import (
    PRESET_FAMILIES_DIR,
    load_correction_knobs,
    load_look_guard,
    load_look_llm,
    load_look_prompt,
)
from shopsteward.editing.edit import run_edit
from shopsteward.editing.live_look import live_look_error, live_look_open
from shopsteward.editing.looks import LookCostCapError
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


def _build_look_adapter(live_look: bool):
    """Live Claude adapter when --live-look is set and the gate is open;
    otherwise the offline fixture. Refuses if --live-look is set but gated off."""
    if not live_look:
        return _default_look_adapter(), False
    if not live_look_open():
        raise typer.BadParameter(live_look_error())
    llm = load_look_llm()
    model = llm.get("model", "")
    pricing = llm.get("pricing") or {}
    if model not in pricing:
        raise typer.BadParameter(
            f"look_llm.model {model!r} has no look_llm.pricing entry; add one so the "
            "monthly soft cap can estimate spend before enabling live generation."
        )
    adapter = OpenRouterLookAdapter(
        api_key=os.environ["OPENROUTER_API_KEY"],
        prompt_template=load_look_prompt(),
        pricing=pricing,
        temperature=float(llm.get("temperature", 0.7)),
        structured=bool(llm.get("structured_output", False)),
    )
    return adapter, True


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
    live_look: Annotated[
        bool, typer.Option(help="Generate a described look via the live LLM (gated)")
    ] = False,
) -> None:
    """Decode each RAW, compute correction + look, write an XMP sidecar."""
    conn = connect(db_path())
    try:
        migrate(conn)
        adapter, is_live = _build_look_adapter(live_look)
        llm = load_look_llm()
        guard = load_look_guard()
        try:
            report = run_edit(
                conn, DEFAULT_USER_ID, Path(path), look,
                decoder=_default_decoder(), look_adapter=adapter,
                model=llm.get("model", model), knobs=load_correction_knobs(),
                regenerate=regenerate, overwrite=overwrite, batch_lock=batch_lock,
                guard_knobs=guard if is_live else None,
                soft_cap_usd=llm.get("monthly_soft_cap_usd") if is_live else None,
                fallback_look=guard.get("fallback_look", "bright-and-true"),
                month_prefix=datetime.now(UTC).strftime("%Y-%m"),
            )
        except (LookCostCapError, LookParseError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(
            f"look={report.look} processed={report.processed} written={report.written} "
            f"skipped_existing={report.skipped_existing} failed={report.failed}"
        )
    finally:
        conn.close()


look_app = typer.Typer(no_args_is_help=True, help="Look tools: A/B preview.")
edit_app.add_typer(look_app, name="look")


@look_app.command("preview")
def look_preview(
    sample_dir: Annotated[str, typer.Argument(help="Folder of a few sample RAWs")],
    look: Annotated[str, typer.Option(help="Candidate look name or description")],
    against: Annotated[str, typer.Option(help="Comparison seed look")] = "bright-and-true",
    live_look: Annotated[
        bool, typer.Option(help="Generate a described candidate via the live LLM")
    ] = False,
) -> None:
    """Write candidate + comparison sidecars into <sample_dir>/_preview/ for LR compare."""
    from shopsteward.editing.config import LOOKS_DIR
    from shopsteward.editing.preview import run_preview

    conn = connect(db_path())
    try:
        migrate(conn)
        adapter, is_live = _build_look_adapter(live_look)
        llm = load_look_llm()
        guard = load_look_guard()
        try:
            out = run_preview(
                conn, DEFAULT_USER_ID, Path(sample_dir), look, against=against,
                decoder=_default_decoder(), look_adapter=adapter,
                model=llm.get("model", "fixture"), knobs=load_correction_knobs(),
                looks_dir=LOOKS_DIR,
                guard_knobs=guard if is_live else None,
                soft_cap_usd=llm.get("monthly_soft_cap_usd") if is_live else None,
                fallback_look=guard.get("fallback_look", "bright-and-true"),
                month_prefix=datetime.now(UTC).strftime("%Y-%m"),
            )
        except (LookCostCapError, LookParseError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"preview: candidate={out['candidate']} vs {out['against']} "
                   f"— {out['frames']} frames in {out['dir']}")
    finally:
        conn.close()
