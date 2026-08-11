"""`shopsteward ops brief`: the read-only shop brief (M8a slice 1, design
§6/§9), plus the autonomy chassis verbs added in PR1 (M8a spec §3):
`run`/`halt`/`resume`/`status`. `approve`/`reject`/`undo` are CLI verbs for
PR3 -- the runner functions they call (runner.approve_action,
.reject_action, .undo_action) already exist and are unit-tested here.

`ops config apply` mirrors `pod config apply` (pod/cli.py) exactly: without
it, editing config/defaults/ops.json on disk has no effect on what
get_ops_config() returns, once seeded once.

`ops run` registers `listing.autorenew_off` (PR2, M8a spec §4) against a
FakeEtsyWriteAdapter by default -- offline, no live Etsy call, ever, unless
the caller passes `--live-autonomy` AND `live_autonomy_open()` is true, in
which case it registers the same capability against a
LiveEtsyWriteAdapter built via `pipeline.listings.push.build_etsy_write_adapter`
(the M5a write path's own token store/construction, reused rather than
duplicated). With `autonomy.enabled=false` (the shipped default) it no-ops
regardless of what is registered."""

from typing import Annotated

import typer

ops_app = typer.Typer(no_args_is_help=True, help="Shop operations brief (M8a).")
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


@ops_app.command("run")
def run_cmd(
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--no-dry-run", help="Report only; never execute")
    ] = True,
    live_autonomy: Annotated[
        bool, typer.Option("--live-autonomy", help="Permit real (non-fake) capability writes")
    ] = False,
) -> None:
    """Run the autonomy chassis once: propose -> govern -> execute for every
    registered capability. Defaults to --dry-run for safety. Registers
    `listing.autorenew_off` against a FakeEtsyWriteAdapter (offline) unless
    `--live-autonomy` is passed AND the gate is open, in which case it is
    registered against a LiveEtsyWriteAdapter instead -- never both, never a
    live adapter constructed on the default path."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter
    from shopsteward.pipeline.live_gate import live_autonomy_error, live_autonomy_open
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.pipeline.ops.registry import REGISTRY, register
    from shopsteward.pipeline.ops.runner import run
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_autonomy and not live_autonomy_open():
        typer.secho(live_autonomy_error(), fg="red")
        raise typer.Exit(code=1)

    adapter = build_etsy_write_adapter(live=live_autonomy)
    if not live_autonomy:
        typer.echo("offline (fake adapter) -- no live Etsy calls will be made.")
    register(ListingAutorenewOff(adapter))

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        ops_config.seed(conn, DEFAULT_USER_ID)
        # dead_listings() (called by listing.autorenew_off.propose()) reads
        # proj_listings for listing titles -- that table only exists once
        # core's own projection has run (`ops brief` precedent above).
        rebuild_core(conn)
        rebuild_ops(conn)
        cfg = ops_config.get_ops_config(conn, DEFAULT_USER_ID)
        if not cfg.autonomy.enabled:
            typer.echo("autonomy.enabled is false -- ops run is a no-op.")
            return
        report = run(conn, DEFAULT_USER_ID, cfg, list(REGISTRY.values()), dry_run=dry_run)
        typer.echo(f"ops run: {report.model_dump()}")
    finally:
        conn.close()


@ops_app.command("halt")
def halt_cmd(
    reason: Annotated[str, typer.Option("--reason", help="Why autonomy is being halted")],
) -> None:
    """Append ops.halted -- every governor call refuses with reason=halted
    until `ops resume` is run."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.ops.runner import halt
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        halt(conn, DEFAULT_USER_ID, reason)
        typer.echo("autonomy halted.")
    finally:
        conn.close()


@ops_app.command("resume")
def resume_cmd(
    reason: Annotated[str, typer.Option("--reason", help="Why autonomy is resuming")] = "",
) -> None:
    """Append ops.resumed, clearing the halt."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.ops.runner import resume
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        resume(conn, DEFAULT_USER_ID, reason)
        typer.echo("autonomy resumed.")
    finally:
        conn.close()


@ops_app.command("status")
def status() -> None:
    """Print autonomy on/off, halt state, month spend vs cap, per-capability
    ladder tier + counters, and today's executed/refused counts."""
    from datetime import UTC, datetime

    from shopsteward.core.db import connect, migrate
    from shopsteward.core.events import read_all
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.governor import is_halted, month_spend
    from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        ops_config.seed(conn, DEFAULT_USER_ID)
        rebuild_ops(conn)
        cfg = ops_config.get_ops_config(conn, DEFAULT_USER_ID)
        today = datetime.now(UTC).date()

        typer.echo(f"autonomy.enabled: {cfg.autonomy.enabled}")
        typer.echo(f"halted: {is_halted(conn, DEFAULT_USER_ID)}")
        spend = month_spend(conn, DEFAULT_USER_ID, today.isoformat()[:7])
        typer.echo(f"month spend: ${spend:.2f} of ${cfg.autonomy.monthly_spend_cap_usd:.2f} cap")

        today_executed = sum(
            1
            for e in read_all(conn, "action.executed")
            if e.user_id == DEFAULT_USER_ID and (e.created_at or "").startswith(today.isoformat())
        )
        today_refused = sum(
            1
            for e in read_all(conn, "action.refused")
            if e.user_id == DEFAULT_USER_ID and (e.created_at or "").startswith(today.isoformat())
        )
        typer.echo(f"today: {today_executed} executed, {today_refused} refused")

        for cap_key, st in capability_states(conn, DEFAULT_USER_ID).items():
            typer.echo(
                f"  {cap_key}: tier={st.tier.name} approvals={st.approvals} "
                f"rejections={st.rejections} undos={st.undos} executions={st.executions} "
                f"tier_since={st.tier_since}"
            )
    finally:
        conn.close()
