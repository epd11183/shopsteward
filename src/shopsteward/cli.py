"""ShopSteward CLI. UI is the primary surface; this is the scriptable path."""

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from dotenv import load_dotenv

if TYPE_CHECKING:
    from shopsteward.adapters.etsy.live import LiveEtsyAdapter

from shopsteward.editing.cli import edit_app
from shopsteward.etsy_cli import etsy_app
from shopsteward.mockups.cli import mockups_app
from shopsteward.pipeline.cli import pipeline_app, score_app
from shopsteward.pipeline.listings.cli import listings_app
from shopsteward.pipeline.listings.pod.cli import pod_app
from shopsteward.pipeline.ops.cli import ops_app

app = typer.Typer(no_args_is_help=True, help="ShopSteward — photography workflow tool.")


def main() -> None:
    """Console-script entry: load .env here (not at import, and not in the
    typer app itself — tests import/invoke `app` and must never inherit the
    operator's real .env). Existing env vars win over .env values."""
    load_dotenv()
    app()


app.add_typer(edit_app, name="edit")
app.add_typer(score_app, name="score")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(mockups_app, name="mockups")
app.add_typer(etsy_app, name="etsy")
app.add_typer(listings_app, name="listings")
app.add_typer(pod_app, name="pod")
app.add_typer(ops_app, name="ops")


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


def _build_live_etsy_adapter() -> "LiveEtsyAdapter":
    """Construct LiveEtsyAdapter from ETSY_API_KEY + on-disk tokens
    (build_etsy_write_adapter precedent, pipeline/listings/push.py). Only
    called after live_etsy_read_open() has already confirmed the
    flag/env/scope -- this fills in the one thing that check doesn't cover:
    shop_id."""
    import os

    from shopsteward.adapters.etsy.auth import EtsyTokenStore
    from shopsteward.adapters.etsy.live import LiveEtsyAdapter

    api_key = os.environ.get("ETSY_API_KEY")
    if not api_key:
        raise RuntimeError("ETSY_API_KEY is not set; live Etsy reads need it.")
    store = EtsyTokenStore()
    tokens = store.load()
    if tokens is None or tokens.shop_id is None:
        raise RuntimeError("No Etsy tokens/shop on disk; run `shopsteward etsy auth` first.")
    access_token = store.get_access_token(api_key)
    return LiveEtsyAdapter(api_key=api_key, shop_id=tokens.shop_id, access_token=access_token)


@app.command()
def sync(
    fixtures: Annotated[
        Path | None, typer.Option(help="Fixture dir (default source until live approved)")
    ] = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Pull from the real Etsy API, read-only (mutually exclusive with --fixtures)",
        ),
    ] = False,
) -> None:
    """Pull Etsy data into the event store and rebuild projections.

    Read-only: get_shop, list_listings, list_receipts -- nothing is ever
    written back to Etsy from this command. --live requires operator
    approval per the triple gate (pipeline.live_gate.live_etsy_read_open)."""
    from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild
    from shopsteward.core.sync import sync_etsy
    from shopsteward.pipeline.live_gate import live_etsy_read_error, live_etsy_read_open
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if fixtures is not None and live:
        typer.secho("Pass --fixtures or --live, not both.", fg="red")
        raise typer.Exit(1)
    if fixtures is None and not live:
        typer.secho(
            "Live Etsy sync is gated on operator approval (PRD §8.4); pass --fixtures or --live.",
            fg="red",
        )
        raise typer.Exit(1)
    if live and not live_etsy_read_open():
        typer.secho(live_etsy_read_error(), fg="red")
        raise typer.Exit(1)

    adapter = _build_live_etsy_adapter() if live else FixtureEtsyAdapter(fixtures)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    result = sync_etsy(conn, adapter, user_id=DEFAULT_USER_ID)
    rebuild(conn)
    if live:
        typer.secho(
            "LIVE Etsy pull (read-only) -- fetched from the real API, not fixtures.", fg="yellow"
        )
    typer.echo(f"synced: {result.model_dump()}")
    typer.echo(
        f"events appended: {result.shops + result.listings + result.receipts} "
        f"(shop={result.shops}, listings={result.listings}, receipts={result.receipts})"
    )
