"""`shopsteward listings` sub-app: build local drafts + print status counts.
No gate3 CLI (UI is the decision surface, mirroring Gate 1) -- mockups/cli.py
convention. `archive_app` (registered as `shopsteward archive` at the root
CLI) is the source-photo-match backfill (design: source-photo-match):
operator-invoked, dry-run by default, never autonomous."""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from shopsteward.adapters.etsy.live import LiveEtsyAdapter

listings_app = typer.Typer(no_args_is_help=True, help="Digital-direct Etsy listing drafts.")
archive_app = typer.Typer(no_args_is_help=True, help="Local archive / source-photo-match backfill.")


@listings_app.command("build")
def build(
    photo_id: Annotated[str | None, typer.Option("--photo-id", help="Limit to one photo")] = None,
    force: Annotated[bool, typer.Option("--force", help="Rebuild even if idempotent")] = False,
    live_copy: Annotated[
        bool, typer.Option("--live-copy", help="Call the real OpenRouter copy API")
    ] = False,
    live_etsy_write: Annotated[
        bool, typer.Option("--live-etsy-write", help="Push drafts to the real Etsy API")
    ] = False,
) -> None:
    """Build local listing drafts for eligible landing files with a
    completed mockup set (run `shopsteward mockups run` first), then push
    every fully-built draft to Etsy (state=draft; Fake adapter by default).
    --force never re-pushes an already-pushed draft."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.live_gate import (
        live_copy_error,
        live_copy_open,
        live_etsy_write_error,
        live_etsy_write_open,
    )
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_copy and not live_copy_open():
        typer.secho(live_copy_error(), fg="red")
        raise typer.Exit(code=1)
    if live_etsy_write and not live_etsy_write_open():
        typer.secho(live_etsy_write_error(), fg="red")
        raise typer.Exit(code=1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        result = build_drafts(
            conn,
            DEFAULT_USER_ID,
            photo_id=photo_id,
            force=force,
            live_copy=live_copy,
            live_etsy_write=live_etsy_write,
        )
        typer.echo(f"listings build: {result.model_dump()}")
    finally:
        conn.close()


@listings_app.command("push")
def push(
    live_etsy_write: Annotated[
        bool, typer.Option("--live-etsy-write", help="Push drafts to the real Etsy API")
    ] = False,
) -> None:
    """Push already-built listing drafts to Etsy without rebuilding
    copy/price/images (`listings build` already does this automatically --
    use this to retry pushing after fixing something, e.g. re-authing with
    the listings_w scope). Idempotent: a draft that already has an Etsy
    listing id (pushed, or partially pushed then push_failed) is left alone
    -- re-running, with or without --force, never re-pushes it and never
    risks a duplicate Etsy listing."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import config as listing_config
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter, push_drafts
    from shopsteward.pipeline.live_gate import live_etsy_write_error, live_etsy_write_open
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_etsy_write and not live_etsy_write_open():
        typer.secho(live_etsy_write_error(), fg="red")
        raise typer.Exit(code=1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        listing_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        cfg = listing_config.get_config(conn, DEFAULT_USER_ID)
        adapter = build_etsy_write_adapter(live=live_etsy_write)
        result = push_drafts(conn, DEFAULT_USER_ID, cfg, adapter)
        typer.echo(f"listings push: {result.model_dump()}")
    finally:
        conn.close()


@listings_app.command("status")
def status() -> None:
    """Print listing draft counts by state."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_listings(conn)

        state_counts = {
            row["state"]: row["n"]
            for row in conn.execute(
                "SELECT state, COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=? "
                "GROUP BY state",
                (DEFAULT_USER_ID,),
            ).fetchall()
        }
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=?", (DEFAULT_USER_ID,)
        ).fetchone()["n"]

        typer.echo(f"Listing drafts: {total}")
        for state, count in sorted(state_counts.items()):
            typer.echo(f"  {state}: {count}")
    finally:
        conn.close()


def _build_live_etsy_read_adapter() -> "LiveEtsyAdapter":
    """Mirrors shopsteward.cli._build_live_etsy_adapter (that helper lives at
    the root CLI, which imports THIS module -- importing it back here would
    be circular, hence the small duplication, same reasoning push.py's own
    build_etsy_write_adapter already uses for the write side)."""
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


@archive_app.command("adopt-local")
def adopt_local_cmd(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    recursive: Annotated[
        bool, typer.Option("--recursive", help="Scan subfolders of `folder` too")
    ] = False,
    fixtures: Annotated[
        Path | None, typer.Option(help="Fixture dir (default source until live approved)")
    ] = None,
    live_etsy_read: Annotated[
        bool, typer.Option("--live-etsy-read", help="Read listing images from the real Etsy API")
    ] = False,
    apply_: Annotated[
        bool, typer.Option("--apply", help="Write archive + linkage events (default: dry-run)")
    ] = False,
    pin: Annotated[
        list[str] | None,
        typer.Option("--pin", help="LISTING_ID=FILE_PATH -- force one listing to adopt one file"),
    ] = None,
    revoke_ids: Annotated[
        list[int] | None,
        typer.Option("--revoke", help="Undo a prior adopt for this listing_id"),
    ] = None,
) -> None:
    """Match local archive photo files against real Etsy listing images via
    perceptual hash, then backfill the source-asset linkage for manual/
    pre-pipeline listings (source_assets.resolve_source() currently returns
    None for these). Dry-run by default: prints a
    `local file -> listing_id -> distance -> verdict` table and writes
    NOTHING. --apply re-runs the identical matching and archives + records
    the linkage for every `match` verdict (never `ambiguous`/`unmatched` --
    those need --pin)."""
    from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import adopt, asset_store_config
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.live_gate import live_etsy_read_error, live_etsy_read_open
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if fixtures is not None and live_etsy_read:
        typer.secho("Pass --fixtures or --live-etsy-read, not both.", fg="red")
        raise typer.Exit(1)
    if fixtures is None and not live_etsy_read:
        typer.secho(
            "Live Etsy read is gated on operator approval (PRD §8.4); "
            "pass --fixtures or --live-etsy-read.",
            fg="red",
        )
        raise typer.Exit(1)
    if live_etsy_read and not live_etsy_read_open():
        typer.secho(live_etsy_read_error(), fg="red")
        raise typer.Exit(1)

    pins: dict[int, str] = {}
    for raw in pin or []:
        listing_id_str, sep, file_path = raw.partition("=")
        if not sep or not file_path:
            typer.secho(f"Bad --pin value {raw!r}; expected LISTING_ID=FILE_PATH.", fg="red")
            raise typer.Exit(1)
        pins[int(listing_id_str)] = file_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        asset_store_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        cfg = asset_store_config.get_asset_store_config(conn, DEFAULT_USER_ID)

        for listing_id in revoke_ids or []:
            if adopt.revoke(conn, DEFAULT_USER_ID, listing_id):
                typer.echo(f"revoked: listing {listing_id}")
            else:
                typer.echo(f"nothing to revoke: listing {listing_id} was never adopted")

        adapter = (
            _build_live_etsy_read_adapter() if live_etsy_read else FixtureEtsyAdapter(fixtures)
        )

        results = adopt.plan_matches(
            conn, DEFAULT_USER_ID, adapter, folder, recursive=recursive, cfg=cfg.match
        )

        typer.echo(f"{'local file':<60} {'listing_id':>10} {'distance':>8}  verdict")
        for r in results:
            distance_str = str(r.distance) if r.distance is not None else "-"
            local = r.local_path or "(none)"
            typer.echo(f"{local:<60} {r.listing_id:>10} {distance_str:>8}  {r.verdict}")
        for listing_id, file_path in pins.items():
            typer.echo(f"{file_path:<60} {listing_id:>10} {'-':>8}  pinned")

        if not apply_:
            typer.echo("Dry-run: nothing written. Pass --apply to adopt confirmed matches.")
            return

        report = adopt.apply_matches(conn, DEFAULT_USER_ID, cfg, results)
        for listing_id, file_path in pins.items():
            if adopt.adopt_one(
                conn,
                DEFAULT_USER_ID,
                cfg,
                listing_id=listing_id,
                local_path=file_path,
                distance=None,
                match_source="pin",
            ):
                report.pinned += 1
                report.adopted += 1
        typer.echo(f"adopt-local: {report.model_dump()}")
    finally:
        conn.close()
