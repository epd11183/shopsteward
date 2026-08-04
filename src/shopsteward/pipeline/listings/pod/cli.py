"""`shopsteward pod config apply`: the rollback lever design §6/§15 and §14's
smoke-test prep assume exists (review fix-up C -- seed() alone has no path
to apply an operator edit to pod.json once it has been seeded once)."""

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
