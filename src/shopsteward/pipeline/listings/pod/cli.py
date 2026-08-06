"""`shopsteward pod config apply`: the rollback lever design §6/§15 and §14's
smoke-test prep assume exists (review fix-up C -- seed() alone has no path
to apply an operator edit to pod.json once it has been seeded once).

`shopsteward pod build [--dry-run]` (slice 2, design §13): builds POD drafts
through print_file_hosted -- provider create/poll/link/enrich is slice 3/4,
so there is no --live-pod flag yet. --dry-run runs the same selection +
pricing computation and prints it; it appends nothing and never reads or
hosts a print file (build_pod_drafts precedent: `listings build` in
listings/cli.py)."""

from typing import Annotated

import typer

pod_app = typer.Typer(no_args_is_help=True, help="POD (print-on-demand) physical listings.")
config_app = typer.Typer(no_args_is_help=True, help="pod.json config.")
pod_app.add_typer(config_app, name="config")


@config_app.command("apply")
def apply() -> None:
    """Re-read config/defaults/pod.json and, if it changed since the last
    seed/apply, append podconfig.updated and rebuild the projection. This is
    the lever behind pod.enabled=false and a markup/base-cost price fix
    (design §15) -- without it, editing the file on disk has no effect on
    what get_pod_config() returns."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings.pod import config as pod_config
    from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        changed = pod_config.apply(conn, DEFAULT_USER_ID)
        rebuild_pod_config(conn)
        typer.echo("pod config updated." if changed else "pod config unchanged.")
    finally:
        conn.close()


@pod_app.command("build")
def build(
    photo_id: Annotated[str | None, typer.Option("--photo-id", help="Limit to one photo")] = None,
    force: Annotated[bool, typer.Option("--force", help="Rebuild even if idempotent")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Compute and print only; append nothing")
    ] = False,
    live_printfile: Annotated[
        bool, typer.Option("--live-printfile", help="Host the print file on the real R2 bucket")
    ] = False,
) -> None:
    """Select variants -> price -> resolve + host the print file for every
    eligible landing file (design §13 slice 2; provider create/link/enrich is
    slice 3/4). --dry-run runs the same selection + pricing and prints it
    without appending anything or reading/hosting a print file."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings.pod.build import build_pod_drafts, dry_run_pod_build
    from shopsteward.pipeline.listings.pod.factory import build_print_file_host
    from shopsteward.pipeline.live_gate import live_printfile_error, live_printfile_open
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_printfile and not live_printfile_open():
        typer.secho(live_printfile_error(), fg="red")
        raise typer.Exit(code=1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)

        if dry_run:
            for result in dry_run_pod_build(conn, DEFAULT_USER_ID, photo_id=photo_id):
                typer.echo(f"{result['landing_file_id']} (photo {result['photo_id']}):")
                for v in result["kept"]:
                    typer.echo(
                        f"  KEEP {v['product_type']}/{v['format']} {v['size']} "
                        f"price={v['retail_price']:.2f} net={v['net']:.2f} "
                        f"margin={v['margin_pct']:.1%}"
                    )
                for d in result["dropped"]:
                    typer.echo(f"  DROP {d['product_type']}/{d['format']} reason={d['reason']}")
            return

        host = build_print_file_host(live=live_printfile)
        report = build_pod_drafts(
            conn, DEFAULT_USER_ID, photo_id=photo_id, force=force, print_file_host=host
        )
        typer.echo(f"pod build: {report.model_dump()}")
    finally:
        conn.close()
