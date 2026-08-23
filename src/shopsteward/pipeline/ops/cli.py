"""`shopsteward ops brief`: the read-only shop brief (M8a slice 1, design
§6/§9), plus the autonomy chassis verbs added in PR1 (M8a spec §3):
`run`/`halt`/`resume`/`status`, and the operator surface added in PR3 (M8a
spec §8 PR3): `approve`/`reject`/`undo`, wiring runner.approve_action/
.reject_action/.undo_action (already unit-tested against the runner
directly in test_e2e_autonomy.py/test_autorenew_capability.py).

`ops approve`/`ops undo` EXECUTE a capability (approve runs the governor
then cap.execute(); undo runs cap.undo()) -- both are adapter-gated exactly
like `ops run`: FakeEtsyWriteAdapter unless --live-autonomy is passed AND
live_autonomy_open() is true. `ops reject` never executes anything, so it
needs no adapter and no gate.

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
regardless of what is registered.

`ops run`/`approve`/`undo` also register `ops.tune_threshold` (PR4, M8a
spec §4) unconditionally -- it holds no adapter (pure `conn` reads/writes of
`opsconfig.updated` events), so it is never gated on `--live-autonomy` and
coexists in the registry alongside `listing.autorenew_off` regardless of
that flag.

`ops run`/`approve`/`undo` also register `listing.gapfill_reprint` (M8b,
gap-fill step 2) unconditionally against the OFFLINE `build_print_file_host
(live=False)` -- its `execute()` only reaches `build_pod_reprint`'s local
`print_file_hosted` stop, never Gelato/Etsy, so it needs no `--live-autonomy`
gate at all. The resulting reprint POD draft reaches Gate 3 (and any live
Gelato/Etsy call) only when the operator next runs the existing, separately
gated `shop build` link step.

`ops run`/`approve`/`undo` also register `social.caption_draft` (M8b slice
6) unconditionally -- it holds NO adapter at all (execute() only ever
appends `social.caption_drafted`; no Meta/IG/FB call of any kind), so it is
never gated on `--live-autonomy` either."""

import os
from typing import Annotated

import typer

ops_app = typer.Typer(no_args_is_help=True, help="Shop operations brief (M8a).")
config_app = typer.Typer(no_args_is_help=True, help="ops.json config.")
ops_app.add_typer(config_app, name="config")


@ops_app.command("brief")
def brief(
    narrate: Annotated[
        bool,
        typer.Option(
            "--narrate/--no-narrate",
            help="Also print an LLM narration of the brief below it (default off, gated)",
        ),
    ] = False,
) -> None:
    """Print the shop brief: revenue vs the prior window, what's selling,
    product/size mix, what's dying, what's trending, what to shoot more of,
    and any data-quality caveats. Deterministic SQL only -- no LLM, no
    network call, of any kind. The deterministic brief is ALWAYS printed as
    the source of truth; --narrate only adds commentary below it (M8b slice
    1, design §5) -- gated on SHOPSTEWARD_LIVE_PLANNER + OPENROUTER_API_KEY,
    and skipped/unavailable are never hard failures."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.live_gate import live_planner_error, live_planner_open
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
        brief_text = render_text(report)
        typer.echo(brief_text)

        if not narrate:
            return

        if not live_planner_open():
            typer.echo("")
            typer.echo(live_planner_error())
            return

        from shopsteward.adapters.planner.openrouter import OpenRouterPlannerAdapter
        from shopsteward.pipeline.ops.planner import narrate_brief
        from shopsteward.pipeline.tuning import get_profile

        soft_cap_usd = get_profile(conn, DEFAULT_USER_ID).vision.monthly_soft_cap_usd
        adapter = OpenRouterPlannerAdapter(
            model=cfg.planner.model,
            api_key=os.environ["OPENROUTER_API_KEY"],
            est_cost_per_mtok=cfg.planner.est_cost_per_mtok,
        )
        narration = narrate_brief(
            conn,
            DEFAULT_USER_ID,
            adapter,
            brief_text,
            soft_cap_usd=soft_cap_usd,
            model=cfg.planner.model,
        )
        typer.echo("")
        if narration is None:
            # ponytail: narrate_brief() collapses "over cap" and "transport
            # error" to the same None -- distinguishing them would need a
            # richer return type; not worth it while this is prose commentary
            # only. Upgrade if the operator ever needs to tell them apart.
            typer.echo("narration skipped: LLM monthly cap reached, or narration unavailable.")
        else:
            typer.echo("-- Claude's read --")
            typer.echo(narration)
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
    from shopsteward.pipeline.listings.pod.factory import build_print_file_host
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter
    from shopsteward.pipeline.live_gate import (
        live_autonomy_error,
        live_autonomy_open,
        live_planner_open,
    )
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
    from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft
    from shopsteward.pipeline.ops.capabilities.deactivate import ListingDeactivate
    from shopsteward.pipeline.ops.capabilities.gapfill import ListingGapfillReprint
    from shopsteward.pipeline.ops.capabilities.renew import ListingRenew
    from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
    from shopsteward.pipeline.ops.capabilities.seo_edit import ListingSeoEdit
    from shopsteward.pipeline.ops.capabilities.tune_threshold import OpsTuneThreshold
    from shopsteward.pipeline.ops.models import ProposedAction
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
    register(ListingReprice(adapter))
    register(ListingSeoEdit(adapter))
    register(ListingDeactivate(adapter))
    register(ListingRenew(adapter))
    register(OpsTuneThreshold())  # no adapter -- registers regardless of --live-autonomy
    # Always the offline print-file host -- this capability's execute() never
    # reaches Gelato/Etsy, so it is never gated on --live-autonomy.
    register(ListingGapfillReprint(build_print_file_host(live=False)))
    # No adapter at all -- execute() only ever appends an event, so this is
    # never gated on --live-autonomy either (no Meta/IG/FB call anywhere).
    register(SocialCaptionDraft())

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

        proposals: list[ProposedAction] | None = None
        if cfg.autonomy.planner_enabled and live_planner_open():
            # Default-off + gated (M8b slice 2, design §7): only here is a
            # planner adapter ever built/called -- the default path above
            # never imports OpenRouter or touches the network.
            from shopsteward.adapters.planner.openrouter import OpenRouterPlannerAdapter
            from shopsteward.pipeline.ops.planner import plan_proposals
            from shopsteward.pipeline.tuning import get_profile

            soft_cap_usd = get_profile(conn, DEFAULT_USER_ID).vision.monthly_soft_cap_usd
            planner_adapter = OpenRouterPlannerAdapter(
                model=cfg.planner.model,
                api_key=os.environ["OPENROUTER_API_KEY"],
                est_cost_per_mtok=cfg.planner.est_cost_per_mtok,
            )
            proposals = plan_proposals(
                conn,
                DEFAULT_USER_ID,
                cfg,
                planner_adapter,
                list(REGISTRY.values()),
                soft_cap_usd=soft_cap_usd,
            )
            typer.echo(f"planner: on ({len(proposals)} proposals)")
        else:
            typer.echo("planner: off (deterministic)")

        report = run(
            conn,
            DEFAULT_USER_ID,
            cfg,
            list(REGISTRY.values()),
            dry_run=dry_run,
            proposals=proposals,
        )
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


def _register_autorenew(live_autonomy: bool) -> None:
    """Shared by `approve`/`undo`: same fake-vs-live construction as `ops
    run` (module docstring). Registers into the module-global REGISTRY so
    approve_action/undo_action can look the capability up by key. Also
    registers `ops.tune_threshold`, which holds no adapter and so is never
    gated on `live_autonomy`."""
    from shopsteward.pipeline.listings.pod.factory import build_print_file_host
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter
    from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
    from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft
    from shopsteward.pipeline.ops.capabilities.deactivate import ListingDeactivate
    from shopsteward.pipeline.ops.capabilities.gapfill import ListingGapfillReprint
    from shopsteward.pipeline.ops.capabilities.renew import ListingRenew
    from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
    from shopsteward.pipeline.ops.capabilities.seo_edit import ListingSeoEdit
    from shopsteward.pipeline.ops.capabilities.tune_threshold import OpsTuneThreshold
    from shopsteward.pipeline.ops.registry import register

    adapter = build_etsy_write_adapter(live=live_autonomy)
    if not live_autonomy:
        typer.echo("offline (fake adapter) -- no live Etsy calls will be made.")
    register(ListingAutorenewOff(adapter))
    register(ListingReprice(adapter))
    register(ListingSeoEdit(adapter))
    register(ListingDeactivate(adapter))
    register(ListingRenew(adapter))
    register(OpsTuneThreshold())
    register(ListingGapfillReprint(build_print_file_host(live=False)))
    register(SocialCaptionDraft())


@ops_app.command("approve")
def approve_cmd(
    action_id: Annotated[str, typer.Argument(help="action_id from `ops brief`'s NEEDS YOU")],
    live_autonomy: Annotated[
        bool,
        typer.Option(
            "--live-autonomy/--no-live-autonomy", help="Permit real (non-fake) capability writes"
        ),
    ] = False,
) -> None:
    """Operator approval of a T2 proposal: runs it through the governor and,
    if approved, executes it (draft §2.4 -- approval is a request to
    execute, not a bypass of the caps). EXECUTES -> adapter-gated exactly
    like `ops run`."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.live_gate import live_autonomy_error, live_autonomy_open
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.pipeline.ops.registry import REGISTRY
    from shopsteward.pipeline.ops.runner import approve_action
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_autonomy and not live_autonomy_open():
        typer.secho(live_autonomy_error(), fg="red")
        raise typer.Exit(code=1)

    _register_autorenew(live_autonomy)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        ops_config.seed(conn, DEFAULT_USER_ID)
        rebuild_core(conn)
        rebuild_ops(conn)
        cfg = ops_config.get_ops_config(conn, DEFAULT_USER_ID)
        try:
            report = approve_action(
                conn, DEFAULT_USER_ID, action_id, list(REGISTRY.values()), cfg=cfg
            )
        except KeyError as exc:
            typer.secho(f"approve failed: {exc}", fg="red")
            raise typer.Exit(code=1) from exc

        if report.executed:
            typer.echo(f"approved and executed: {action_id}")
        elif report.failed:
            typer.echo(f"approved but execution failed: {action_id}")
        elif report.refused:
            typer.echo(f"approved but refused by the governor: {action_id}")
        else:
            typer.echo(f"no-op: {action_id}")
    finally:
        conn.close()


@ops_app.command("reject")
def reject_cmd(
    action_id: Annotated[str, typer.Argument(help="action_id from `ops brief`'s NEEDS YOU")],
) -> None:
    """Operator rejection of a T2 proposal: records action.rejected, demotes
    the capability one tier, and resets its ladder counters (draft §2.4,
    asymmetric/immediate). Never executes anything -- no adapter, no
    live-write gate."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.pipeline.ops.runner import reject_action
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        ops_config.seed(conn, DEFAULT_USER_ID)
        rebuild_core(conn)
        rebuild_ops(conn)
        try:
            reject_action(conn, DEFAULT_USER_ID, action_id)
        except KeyError as exc:
            typer.secho(f"reject failed: {exc}", fg="red")
            raise typer.Exit(code=1) from exc
        typer.echo(f"rejected: {action_id}")
    finally:
        conn.close()


@ops_app.command("undo")
def undo_cmd(
    action_id: Annotated[str, typer.Argument(help="action_id from `ops brief`'s DONE")],
    live_autonomy: Annotated[
        bool,
        typer.Option(
            "--live-autonomy/--no-live-autonomy", help="Permit real (non-fake) capability writes"
        ),
    ] = False,
) -> None:
    """Undo a previously-executed action: runs cap.undo(), records
    action.undone, and demotes the capability one tier with counters reset
    (runner.undo_action). EXECUTES cap.undo() -> adapter-gated exactly like
    `ops run`/`ops approve`."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.events import read_all
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.live_gate import live_autonomy_error, live_autonomy_open
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.pipeline.ops.registry import REGISTRY
    from shopsteward.pipeline.ops.runner import undo_action
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_autonomy and not live_autonomy_open():
        typer.secho(live_autonomy_error(), fg="red")
        raise typer.Exit(code=1)

    _register_autorenew(live_autonomy)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        ops_config.seed(conn, DEFAULT_USER_ID)
        rebuild_core(conn)
        rebuild_ops(conn)
        try:
            undo_action(conn, DEFAULT_USER_ID, action_id, list(REGISTRY.values()))
        except (KeyError, ValueError) as exc:
            # KeyError: unknown action_id/capability. ValueError: a
            # no-undo capability (runner.undo_action's guard) -- both are
            # clean, non-zero-exit messages, never a traceback.
            typer.secho(f"undo failed: {exc}", fg="red")
            raise typer.Exit(code=1) from exc

        restored_to = None
        for e in read_all(conn, "action.undone"):
            if e.user_id == DEFAULT_USER_ID and e.payload.get("action_id") == action_id:
                restored_to = e.payload["restored_to"]
        typer.echo(f"undone: {action_id} -- restored to {restored_to}")
    finally:
        conn.close()
