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
) -> None:
    """Build local listing drafts for eligible landing files with a
    completed mockup set (run `shopsteward mockups run` first)."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.live_gate import live_copy_error, live_copy_open
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_copy and not live_copy_open():
        typer.secho(live_copy_error(), fg="red")
        raise typer.Exit(code=1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        result = build_drafts(
            conn, DEFAULT_USER_ID, photo_id=photo_id, force=force, live_copy=live_copy
        )
        typer.echo(f"listings build: {result.model_dump()}")
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
