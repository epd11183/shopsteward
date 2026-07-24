"""`shopsteward listings` sub-app: build local drafts + print status counts.
No gate3 CLI (UI is the decision surface, mirroring Gate 1) -- mockups/cli.py
convention."""

from typing import Annotated

import typer

listings_app = typer.Typer(no_args_is_help=True, help="Digital-direct Etsy listing drafts.")


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
