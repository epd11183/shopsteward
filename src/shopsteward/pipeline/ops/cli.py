"""`shopsteward ops brief`: the read-only shop brief (M8a slice 1, design
§6/§9). No `run`/`approve`/`halt`/`status` here -- those need the autonomy
chassis (registry/governor/runner/ladder) from slices 2+.

`ops config apply` mirrors `pod config apply` (pod/cli.py) exactly: without
it, editing config/defaults/ops.json on disk has no effect on what
get_ops_config() returns, once seeded once."""

import typer

ops_app = typer.Typer(no_args_is_help=True, help="Shop operations brief (read-only, M8a slice 1).")
config_app = typer.Typer(no_args_is_help=True, help="ops.json config.")
ops_app.add_typer(config_app, name="config")


@ops_app.command("brief")
def brief() -> None:
    """Print the shop brief: revenue vs the prior window, what's selling,
    product/size mix, what's dying, what's trending, what to shoot more of,
    and any data-quality caveats. Deterministic SQL only -- no LLM, no
    network call, of any kind."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.brief import generate_brief, render_text
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        ops_config.seed(conn, DEFAULT_USER_ID)
        rebuild_core(conn)
        rebuild_ops(conn)
        cfg = ops_config.get_ops_config(conn, DEFAULT_USER_ID)
        report = generate_brief(conn, DEFAULT_USER_ID, cfg)
        typer.echo(render_text(report))
    finally:
        conn.close()


@config_app.command("apply")
def apply() -> None:
    """Re-read config/defaults/ops.json and, if it changed since the last
    seed/apply, append opsconfig.updated and rebuild the projection."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        changed = ops_config.apply(conn, DEFAULT_USER_ID)
        rebuild_ops(conn)
        typer.echo("ops config updated." if changed else "ops config unchanged.")
    finally:
        conn.close()
