"""`shopsteward score` sub-app: run the scoring pipeline over awaiting_scoring
hero photos and queue passers for Gate 1."""

import os
from pathlib import Path
from typing import Annotated

import typer

_REPO_ROOT = Path(__file__).resolve().parents[3]
TUNING_PROFILE_PATH = _REPO_ROOT / "config" / "defaults" / "tuning_profile.json"
COMMERCIAL_PROMPT_PATH = _REPO_ROOT / "config" / "defaults" / "prompts" / "commercial_score.txt"

score_app = typer.Typer(no_args_is_help=True, help="Hero scoring pipeline.")


@score_app.command("run")
def run(
    limit: Annotated[int | None, typer.Option(help="Max photos to score this run")] = None,
    live_vision: Annotated[
        bool, typer.Option("--live-vision", help="Call the real Gemini vision API")
    ] = False,
) -> None:
    """Score awaiting_scoring hero photos; queue composite>=threshold for Gate 1."""
    if live_vision and (
        os.environ.get("SHOPSTEWARD_LIVE_VISION") != "1" or not os.environ.get("GEMINI_API_KEY")
    ):
        typer.secho(
            "Live vision scoring is gated on operator approval (PRD §8.4): set "
            "SHOPSTEWARD_LIVE_VISION=1 and GEMINI_API_KEY in the environment, "
            "then re-run with --live-vision.",
            fg="red",
        )
        raise typer.Exit(code=1)

    from shopsteward.adapters.vision.fake import FixtureVisionAdapter
    from shopsteward.adapters.vision.gemini import GeminiVisionAdapter
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline import tuning
    from shopsteward.pipeline.scoring import run_scoring
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        tuning.seed(conn, DEFAULT_USER_ID, TUNING_PROFILE_PATH)
        profile = tuning.get_profile(conn, DEFAULT_USER_ID)

        if live_vision:
            vision = GeminiVisionAdapter(
                api_key=os.environ["GEMINI_API_KEY"],
                prompt=COMMERCIAL_PROMPT_PATH.read_text(),
                pricing=profile.vision.est_cost_per_mtok,
            )
        else:
            vision = FixtureVisionAdapter()

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
