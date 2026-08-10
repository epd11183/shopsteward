"""`shopsteward pipeline` sub-app: landing-folder scan + status."""

import typer

pipeline_app = typer.Typer(no_args_is_help=True, help="Landing-folder utilities.")


@pipeline_app.command("scan")
def scan() -> None:
    """Scan the landing folder for new/invalid TIFF & JPEG files."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline import landing, tuning
    from shopsteward.pipeline.config import TUNING_PROFILE_PATH
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        tuning.seed(conn, DEFAULT_USER_ID, TUNING_PROFILE_PATH)
        report = landing.scan_landing(conn, DEFAULT_USER_ID)
        typer.echo(f"landing scan: {report.model_dump()}")
    finally:
        conn.close()


@pipeline_app.command("status")
def status() -> None:
    """Print landing counts and the UI URL."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.projections import rebuild_pipeline
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_pipeline(conn)

        landing_counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM proj_landing_files WHERE user_id = ? "
                "GROUP BY status",
                (DEFAULT_USER_ID,),
            ).fetchall()
        }
        manual_drops = conn.execute(
            "SELECT COUNT(*) AS n FROM proj_landing_files "
            "WHERE user_id = ? AND status = 'valid' AND photo_id IS NULL",
            (DEFAULT_USER_ID,),
        ).fetchone()["n"]

        typer.echo("Landing:")
        typer.echo(f"  valid: {landing_counts.get('valid', 0)}")
        typer.echo(f"  invalid: {landing_counts.get('invalid', 0)}")
        typer.echo(f"  manual: {manual_drops}")
        typer.echo("UI: http://127.0.0.1:8321")
    finally:
        conn.close()
