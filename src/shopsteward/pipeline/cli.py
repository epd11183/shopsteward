"""`shopsteward score` sub-app: run the scoring pipeline over awaiting_scoring
hero photos and queue passers for Gate 1. `shopsteward pipeline` sub-app:
landing-folder scan + status. No gate1 CLI -- the UI is the decision surface."""

from typing import Annotated

import typer

from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.live_gate import live_vision_error, live_vision_open

score_app = typer.Typer(no_args_is_help=True, help="Hero scoring pipeline.")
pipeline_app = typer.Typer(no_args_is_help=True, help="Gate 1 + landing-folder utilities.")


@score_app.command("run")
def run(
    limit: Annotated[int | None, typer.Option(help="Max photos to score this run")] = None,
    live_vision: Annotated[
        bool, typer.Option("--live-vision", help="Call the real Gemini vision API")
    ] = False,
) -> None:
    """Score awaiting_scoring hero photos; queue composite>=threshold for Gate 1."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline import tuning
    from shopsteward.pipeline.scoring import run_scoring
    from shopsteward.pipeline.vision_factory import build_vision_adapter
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        tuning.seed(conn, DEFAULT_USER_ID, TUNING_PROFILE_PATH)
        profile = tuning.get_profile(conn, DEFAULT_USER_ID)

        if live_vision and not live_vision_open(profile.vision.provider):
            typer.secho(live_vision_error(profile.vision.provider), fg="red")
            raise typer.Exit(code=1)

        vision = build_vision_adapter(profile, live=live_vision)

        result = run_scoring(conn, DEFAULT_USER_ID, vision, limit=limit, live=live_vision)
        typer.echo(f"scored: {result.model_dump()}")
        if result.cap_hit:
            typer.secho(
                "monthly vision soft cap reached; run stopped early, remaining photos "
                "stay eligible for the next run",
                fg="yellow",
            )
    finally:
        conn.close()


@pipeline_app.command("scan")
def scan() -> None:
    """Scan the landing folder for new/invalid TIFF & JPEG files."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline import landing, tuning
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
    """Print Gate 1 queue counts, landing counts, and the UI URL."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.projections import rebuild_pipeline
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_pipeline(conn)

        gate1_counts = {
            row["state"]: row["n"]
            for row in conn.execute(
                "SELECT state, COUNT(*) AS n FROM proj_gate1 WHERE user_id = ? GROUP BY state",
                (DEFAULT_USER_ID,),
            ).fetchall()
        }
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

        typer.echo("Gate 1 queue:")
        for state in ("pending", "snoozed", "approved", "rejected"):
            typer.echo(f"  {state}: {gate1_counts.get(state, 0)}")
        typer.echo("Landing:")
        typer.echo(f"  valid: {landing_counts.get('valid', 0)}")
        typer.echo(f"  invalid: {landing_counts.get('invalid', 0)}")
        typer.echo(f"  manual: {manual_drops}")
        typer.echo("UI: http://127.0.0.1:8321")
    finally:
        conn.close()
