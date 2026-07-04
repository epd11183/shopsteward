"""`shopsteward mockups` sub-app: run the mockup job, scan the template
registry, and print status counts."""

from typing import Annotated

import typer

mockups_app = typer.Typer(no_args_is_help=True, help="Staging template mockup pipeline.")


@mockups_app.command("run")
def run(
    photo_id: Annotated[str | None, typer.Option("--photo-id", help="Limit to one photo")] = None,
    force: Annotated[bool, typer.Option("--force", help="Regenerate even if idempotent")] = False,
) -> None:
    """Render mockup sets for eligible landing files."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.mockups.jobs import run_mockups
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        result = run_mockups(conn, DEFAULT_USER_ID, photo_id=photo_id, force=force)
        typer.echo(f"mockups run: {result.model_dump()}")
    finally:
        conn.close()


@mockups_app.command("templates-scan")
def templates_scan() -> None:
    """Scan the staging template registry (defaults + operator dirs)."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.mockups.templates import scan_templates
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        report = scan_templates(conn, DEFAULT_USER_ID)
        typer.echo(f"templates scan: {report.model_dump()}")
    finally:
        conn.close()


@mockups_app.command("status")
def status() -> None:
    """Print template registry and mockup counts."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.mockups.projections import rebuild_mockups
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_mockups(conn)

        template_counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM proj_staging_templates WHERE user_id=? "
                "GROUP BY status",
                (DEFAULT_USER_ID,),
            ).fetchall()
        }
        sets_count = conn.execute(
            "SELECT COUNT(*) AS n FROM proj_mockup_sets WHERE user_id=?", (DEFAULT_USER_ID,)
        ).fetchone()["n"]
        mockups_count = conn.execute(
            "SELECT COUNT(*) AS n FROM proj_mockups WHERE user_id=?", (DEFAULT_USER_ID,)
        ).fetchone()["n"]

        typer.echo("Templates:")
        typer.echo(f"  valid: {template_counts.get('valid', 0)}")
        typer.echo(f"  invalid: {template_counts.get('invalid', 0)}")
        typer.echo(f"Mockup sets: {sets_count}")
        typer.echo(f"Mockups: {mockups_count}")
    finally:
        conn.close()
