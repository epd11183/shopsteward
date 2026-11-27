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
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    import sqlite3
    from datetime import date

    from shopsteward.adapters.buffer.interface import BufferAdapter
    from shopsteward.adapters.etsy.interface import EtsyWriteAdapter
    from shopsteward.adapters.etsy.live import LiveEtsyAdapter
    from shopsteward.pipeline.ops.buffer_publish import BufferPublisherAdapter
    from shopsteward.pipeline.ops.capabilities.catalog_expand import ListingCatalogExpand
    from shopsteward.pipeline.ops.models import OpsConfig
    from shopsteward.pipeline.ops.registry import Capability

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
        _register_autorenew(False, announce=False)  # effective tiers in AUTONOMY
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


@ops_app.command("probe-keyword")
def probe_keyword_cmd(
    phrase: Annotated[
        list[str], typer.Argument(help="One or more phrases to probe (space-separated)")
    ],
    taxonomy_id: Annotated[
        int | None, typer.Option(help="Etsy taxonomy_id filter, same for every phrase")
    ] = None,
    min_price: Annotated[float | None, typer.Option(help="Minimum price filter (USD)")] = None,
    max_price: Annotated[float | None, typer.Option(help="Maximum price filter (USD)")] = None,
    fixtures: Annotated[
        Path | None, typer.Option(help="Fixture dir (default source until live approved)")
    ] = None,
    live_etsy_read: Annotated[
        bool,
        typer.Option("--live-etsy-read", help="Query the real Etsy findAllListingsActive API"),
    ] = False,
) -> None:
    """Probe Etsy's free findAllListingsActive competition/relevance signal
    for one or more keyword phrases: competition count, the tag set Etsy's
    own ranker rewards, price range, and a favorites-per-day demand proxy --
    computed over the top-ranked results with our own listings excluded.
    Read-only market research, no autonomy chassis involved: never spends,
    never writes to Etsy. Appends one `etsy.keyword.probed` event per
    phrase (derived aggregates only -- see keyword_probe.py's module
    docstring for exactly what is never persisted). Fixture-backed by
    default, same --fixtures/--live-etsy-read gating as
    `shop archive adopt-local`."""
    from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.live_gate import live_etsy_read_error, live_etsy_read_open
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.keyword_probe import probe_keyword
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if fixtures is not None and live_etsy_read:
        typer.secho("Pass --fixtures or --live-etsy-read, not both.", fg="red")
        raise typer.Exit(1)
    if fixtures is None and not live_etsy_read:
        typer.secho(
            "Live Etsy read is gated on operator approval (PRD §8.4); "
            "pass --fixtures or --live-etsy-read.",
            fg="red",
        )
        raise typer.Exit(1)
    if live_etsy_read and not live_etsy_read_open():
        typer.secho(live_etsy_read_error(), fg="red")
        raise typer.Exit(1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        ops_config.seed(conn, DEFAULT_USER_ID)
        rebuild_core(conn)
        rebuild_ops(conn)
        cfg = ops_config.get_ops_config(conn, DEFAULT_USER_ID)

        phrases = phrase[: cfg.keyword_probe.max_phrases_per_run]
        if len(phrases) < len(phrase):
            typer.secho(
                f"only probing the first {cfg.keyword_probe.max_phrases_per_run} of "
                f"{len(phrase)} phrases (keyword_probe.max_phrases_per_run)",
                fg="yellow",
            )

        adapter = (
            _build_live_etsy_read_adapter() if live_etsy_read else FixtureEtsyAdapter(fixtures)
        )

        for p in phrases:
            result = probe_keyword(
                conn,
                DEFAULT_USER_ID,
                adapter,
                cfg,
                p,
                taxonomy_id=taxonomy_id,
                min_price=min_price,
                max_price=max_price,
            )
            agg = result.aggregates
            typer.echo(f"\n== {p!r} ==")
            typer.echo(f"  competition (matching listings): {result.competition_count}")
            typer.echo(f"  sample (top-{result.top_n}, own listings excluded): {agg.sample_size}")
            if agg.sample_size == 0:
                typer.echo("  no non-own results -- price/favorites unmeasurable")
                continue
            top_tags = sorted(agg.tag_frequency.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            typer.echo(f"  top tags: {', '.join(f'{t} ({n})' for t, n in top_tags)}")
            typer.echo(
                f"  price USD: median {agg.median_price_usd} "
                f"(min {agg.min_price_usd}, max {agg.max_price_usd})"
            )
            typer.echo(
                f"  favorites/day: median {agg.median_favorites_per_day} "
                f"(min {agg.min_favorites_per_day}, max {agg.max_favorites_per_day})"
            )
    finally:
        conn.close()


def _build_live_etsy_read_adapter() -> "LiveEtsyAdapter":
    """Duplicated (not imported) from pipeline.listings.cli's identical
    helper -- same reasoning that module's own docstring gives for
    duplicating push.py's build_etsy_write_adapter: avoiding a needless
    cross-module import for four lines, not a circular-import necessity
    here specifically, but keeping the same shape/behavior everywhere this
    pattern appears."""
    import os

    from shopsteward.adapters.etsy.auth import EtsyTokenStore
    from shopsteward.adapters.etsy.live import LiveEtsyAdapter

    api_key = os.environ.get("ETSY_API_KEY")
    if not api_key:
        raise RuntimeError("ETSY_API_KEY is not set; live Etsy reads need it.")
    store = EtsyTokenStore()
    tokens = store.load()
    if tokens is None or tokens.shop_id is None:
        raise RuntimeError("No Etsy tokens/shop on disk; run `shopsteward etsy auth` first.")
    access_token = store.get_access_token(api_key)
    return LiveEtsyAdapter(api_key=api_key, shop_id=tokens.shop_id, access_token=access_token)


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
    capability: Annotated[
        str | None,
        typer.Option(
            "--capability",
            help="Run ONLY this capability's propose() (e.g. listing.size_claims_fix); "
            "the planner LLM is skipped",
        ),
    ] = None,
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
    from shopsteward.pipeline.ops.capabilities.autorenew_on import ListingAutorenewOn
    from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft
    from shopsteward.pipeline.ops.capabilities.deactivate import ListingDeactivate
    from shopsteward.pipeline.ops.capabilities.file_replace import ListingFileReplace
    from shopsteward.pipeline.ops.capabilities.gapfill import ListingGapfillReprint
    from shopsteward.pipeline.ops.capabilities.panel_replace import ListingPanelReplace
    from shopsteward.pipeline.ops.capabilities.pinterest_post import SocialPinterestPost
    from shopsteward.pipeline.ops.capabilities.renew import ListingRenew
    from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
    from shopsteward.pipeline.ops.capabilities.seo_edit import ListingSeoEdit
    from shopsteward.pipeline.ops.capabilities.size_claims_fix import ListingSizeClaimsFix
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
    register(ListingAutorenewOn(adapter))
    register(ListingReprice(adapter))
    register(ListingSeoEdit(adapter))
    register(ListingSizeClaimsFix(adapter))
    register(ListingPanelReplace(adapter))
    register(ListingFileReplace(adapter))
    register(ListingDeactivate(adapter))
    register(ListingRenew(adapter))
    register(OpsTuneThreshold())  # no adapter -- registers regardless of --live-autonomy
    # T11 -- same adapter as the other Etsy-write capabilities above;
    # live_copy resolves independently (SHOPSTEWARD_LIVE_COPY, its own gate)
    # and is the sole precondition_ok control (module docstring).
    register(_catalog_expand(adapter))
    # Always the offline print-file host -- this capability's execute() never
    # reaches Gelato/Etsy, so it is never gated on --live-autonomy.
    register(ListingGapfillReprint(build_print_file_host(live=False)))
    # No adapter at all -- execute() only ever appends an event, so this is
    # never gated on --live-autonomy either (no Meta/IG/FB call anywhere).
    # Variant A (draft-only) unless live-autonomy AND the Buffer gates are open.
    register(SocialCaptionDraft(_build_buffer_publisher(live_autonomy)))
    register(SocialPinterestPost(_build_buffer_publisher(live_autonomy)))

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
        capabilities = list(REGISTRY.values())
        if capability is not None:
            capabilities = [c for c in capabilities if c.key == capability]
            if not capabilities:
                typer.secho(f"unknown capability: {capability}", fg="red")
                raise typer.Exit(code=1)
            typer.echo(f"only {capability}: planner skipped")
        elif cfg.autonomy.planner_enabled and live_planner_open():
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
            capabilities,
            dry_run=dry_run,
            proposals=proposals,
            live_autonomy=live_autonomy,
            # Only a full LIVE run is the weekly run (heartbeat).
            record_completion=capability is None and live_autonomy,
        )
        typer.echo(f"ops run: {report.model_dump(exclude={'would_propose'})}")
        for a in report.would_propose:
            typer.echo(f"  would propose: {a.capability} {a.target_id} ({a.action_id})")
        if report.live_gate_blocked:
            # Finding 2: without --live-autonomy, a live-gated action that
            # would otherwise auto-execute (AUTO/NOTIFY tier) is refused
            # BEFORE touching the fake adapter, left "proposed" -- never
            # action.failed. Retry with --live-autonomy once ready.
            typer.secho(
                "live autonomy gate not set -- one or more live-gated actions were "
                "left pending instead of executing (retry with --live-autonomy once ready).",
                fg="yellow",
            )
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


@ops_app.command("panel-unblock")
def panel_unblock_cmd(
    listing_id: Annotated[int, typer.Argument(help="Etsy listing_id blocked as panel-orphaned")],
    reason: Annotated[str, typer.Option("--reason", help="How the listing was reconciled")],
) -> None:
    """Release a listing `listing.panel_replace` stopped proposing after both
    panel images went live (append-only: records listingdraft.panel_unorphaned).
    Reconcile the Etsy gallery by hand FIRST."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.ops.capabilities.panel_replace import unblock
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        if not unblock(conn, DEFAULT_USER_ID, listing_id, reason):
            typer.secho(f"listing {listing_id} is not panel-orphaned.", fg="red")
            raise typer.Exit(code=1)
        typer.echo(f"listing {listing_id} unblocked for panel_replace.")
    finally:
        conn.close()


@ops_app.command("heartbeat")
def heartbeat(
    max_age_days: Annotated[
        float, typer.Option("--max-age-days", help="Alert if the last live ops run is older")
    ] = 8.0,
) -> None:
    """Exit 1 if the weekly run hasn't happened: the newest `ops.run.completed`
    (appended by every live `ops run`) dates the last live ops run. A sync
    alone does not count (a daily `sync --live` also appends etsy.shop.observed).
    Until the first `ops.run.completed` exists, falls back to the newest
    etsy.shop.observed so it doesn't fire falsely right after deploy."""
    from datetime import UTC, datetime

    from shopsteward.core.db import connect
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    conn = connect(db_path())
    try:
        last = None
        for event_type in ("ops.run.completed", "etsy.shop.observed"):
            last = conn.execute(
                "SELECT MAX(created_at) FROM events WHERE user_id=? AND type=?",
                (DEFAULT_USER_ID, event_type),
            ).fetchone()[0]
            if last is not None:
                break
    finally:
        conn.close()
    if last is None:
        typer.echo("STALE: no live ops run has ever happened")
        raise typer.Exit(1)
    age = datetime.now(UTC) - datetime.fromisoformat(last.replace("Z", "+00:00"))
    days = age.total_seconds() / 86400
    if days > max_age_days:
        typer.echo(f"STALE: last live ops run {days:.1f} days ago ({last})")
        raise typer.Exit(1)
    typer.echo(f"ok: last live ops run {days:.1f} days ago ({last})")


@ops_app.command("status")
def status() -> None:
    """Print autonomy on/off, halt state, month spend vs cap, per-capability
    ladder tier + counters, and today's executed/refused counts."""
    from datetime import UTC, datetime

    from shopsteward.core.db import connect, migrate
    from shopsteward.core.events import read_all
    from shopsteward.pipeline.llm_ledger import monthly_spend
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.governor import is_halted, month_spend_parts
    from shopsteward.pipeline.ops.projections import capability_states, rebuild_ops
    from shopsteward.pipeline.ops.tiers import effective_tier_for
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
        acts, fees = month_spend_parts(conn, DEFAULT_USER_ID, today.isoformat()[:7], cfg)
        llm = monthly_spend(conn, DEFAULT_USER_ID, today.isoformat()[:7])
        typer.echo(
            f"month spend: ${acts + fees:.2f} of ${cfg.autonomy.monthly_spend_cap_usd:.2f} cap "
            f"(actions ${acts:.2f}, listing fees ${fees:.2f}); LLM ${llm:.2f} (separate ledger)"
        )

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

        _register_autorenew(False, announce=False)
        for cap_key, st in capability_states(conn, DEFAULT_USER_ID).items():
            eff = effective_tier_for(cap_key, st, cfg.autonomy.tier_floor)
            ladder = "" if eff == st.tier else f" (ladder {st.tier.name})"
            typer.echo(
                f"  {cap_key}: tier={eff.name}{ladder} approvals={st.approvals} "
                f"rejections={st.rejections} undos={st.undos} executions={st.executions} "
                f"tier_since={st.tier_since}"
            )
    finally:
        conn.close()


def _catalog_expand(adapter: "EtsyWriteAdapter") -> "ListingCatalogExpand":
    """`listing.catalog_expand` with its two independent live gates resolved:
    SHOPSTEWARD_LIVE_COPY (copy generation) and SHOPSTEWARD_LIVE_VISION (photo
    scoring + the read-back copy-vs-photo check). Either closed -> precondition_ok False, and
    the fixture checker (which never approves) is wired."""
    import json

    from shopsteward.pipeline.config import TUNING_PROFILE_PATH
    from shopsteward.pipeline.live_gate import live_copy_open, live_vision_open
    from shopsteward.pipeline.models import TuningProfile
    from shopsteward.pipeline.ops.capabilities.catalog_expand import ListingCatalogExpand
    from shopsteward.pipeline.vision_factory import build_copy_checker, build_vision_adapter

    profile = TuningProfile.model_validate(
        json.loads(TUNING_PROFILE_PATH.read_text(encoding="utf-8"))
    )
    live_check = profile.vision.provider == "openrouter" and live_vision_open("openrouter")
    return ListingCatalogExpand(
        adapter,
        live_copy=live_copy_open(),
        checker=build_copy_checker(profile, live_check),
        live_check=live_check,
        vision=build_vision_adapter(profile, live_check),
        live_vision=live_check,
    )


def _register_autorenew(live_autonomy: bool, *, announce: bool = True) -> None:
    """Shared by `approve`/`undo`: same fake-vs-live construction as `ops
    run` (module docstring). Registers into the module-global REGISTRY so
    approve_action/undo_action can look the capability up by key. Also
    registers `ops.tune_threshold`, which holds no adapter and so is never
    gated on `live_autonomy`."""
    from shopsteward.pipeline.listings.pod.factory import build_print_file_host
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter
    from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
    from shopsteward.pipeline.ops.capabilities.autorenew_on import ListingAutorenewOn
    from shopsteward.pipeline.ops.capabilities.caption_draft import SocialCaptionDraft
    from shopsteward.pipeline.ops.capabilities.deactivate import ListingDeactivate
    from shopsteward.pipeline.ops.capabilities.file_replace import ListingFileReplace
    from shopsteward.pipeline.ops.capabilities.gapfill import ListingGapfillReprint
    from shopsteward.pipeline.ops.capabilities.panel_replace import ListingPanelReplace
    from shopsteward.pipeline.ops.capabilities.pinterest_post import SocialPinterestPost
    from shopsteward.pipeline.ops.capabilities.renew import ListingRenew
    from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
    from shopsteward.pipeline.ops.capabilities.seo_edit import ListingSeoEdit
    from shopsteward.pipeline.ops.capabilities.size_claims_fix import ListingSizeClaimsFix
    from shopsteward.pipeline.ops.capabilities.tune_threshold import OpsTuneThreshold
    from shopsteward.pipeline.ops.registry import register

    adapter = build_etsy_write_adapter(live=live_autonomy)
    if not live_autonomy and announce:
        typer.echo("offline (fake adapter) -- no live Etsy calls will be made.")
    register(ListingAutorenewOff(adapter))
    register(ListingAutorenewOn(adapter))
    register(ListingReprice(adapter))
    register(ListingSeoEdit(adapter))
    register(ListingSizeClaimsFix(adapter))
    register(ListingPanelReplace(adapter))
    register(ListingFileReplace(adapter))
    register(ListingDeactivate(adapter))
    register(ListingRenew(adapter))
    register(OpsTuneThreshold())
    register(_catalog_expand(adapter))
    register(ListingGapfillReprint(build_print_file_host(live=False)))
    register(SocialCaptionDraft(_build_buffer_publisher(live_autonomy)))
    register(SocialPinterestPost(_build_buffer_publisher(live_autonomy)))


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
                conn,
                DEFAULT_USER_ID,
                action_id,
                list(REGISTRY.values()),
                cfg=cfg,
                live_autonomy=live_autonomy,
            )
        except KeyError as exc:
            typer.secho(f"approve failed: {exc}", fg="red")
            raise typer.Exit(code=1) from exc

        if report.live_gate_blocked:
            # E11: refuse loudly, leave the proposal exactly as it was
            # (still "proposed", still approvable once --live-autonomy is
            # set) -- never execute against a fresh, empty fake adapter.
            typer.secho(
                "live autonomy gate not set -- refusing to execute against the fake "
                f"adapter; proposal left pending: {action_id} "
                "(retry with --live-autonomy once ready)",
                fg="red",
            )
            raise typer.Exit(code=1)
        elif report.executed:
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


def _build_buffer_adapter(live: bool) -> "BufferAdapter":
    if not live:
        from shopsteward.adapters.buffer.fake import FakeBufferAdapter

        return FakeBufferAdapter()
    from shopsteward.adapters.buffer.live import LiveBufferAdapter

    return LiveBufferAdapter(os.environ["BUFFER_API_KEY"])


def _build_buffer_publisher(live_autonomy: bool) -> "BufferPublisherAdapter | None":
    """The live Buffer adapter only under `--live-autonomy` AND the Buffer WRITE
    gate (SHOPSTEWARD_LIVE_BUFFER=1 + SHOPSTEWARD_LIVE_BUFFER_WRITE=1 + key);
    otherwise None, i.e. draft-only Variant A. Never a fake in production code."""
    from shopsteward.pipeline.live_gate import live_buffer_write_open

    if not (live_autonomy and live_buffer_write_open()):
        return None
    from shopsteward.adapters.buffer.live import LiveBufferAdapter

    return LiveBufferAdapter(os.environ["BUFFER_API_KEY"])


@ops_app.command("buffer-sync")
def buffer_sync_cmd(
    live: Annotated[
        bool,
        typer.Option("--live", help="Read from Buffer (needs SHOPSTEWARD_LIVE_BUFFER=1 + key)."),
    ] = False,
    since_days: Annotated[int, typer.Option("--since-days", min=1)] = 35,
) -> None:
    """Observe Buffer posts (read-only): record status/metrics changes and
    mark sent pins/captions posted at their real send time. Offline default
    uses an empty fake adapter."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.live_gate import live_buffer_error, live_buffer_open
    from shopsteward.pipeline.ops.buffer_sync import sync_buffer
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live and not live_buffer_open():
        typer.secho(live_buffer_error(), fg="red")
        raise typer.Exit(1)
    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        res = sync_buffer(conn, DEFAULT_USER_ID, _build_buffer_adapter(live), since_days=since_days)
    finally:
        conn.close()
    typer.echo("buffer-sync (" + ("live" if live else "offline fake") + ")")
    for key in sorted(res.seen):
        typer.echo(f"  {key}: {res.seen[key]}")
    typer.echo(f"  newly recorded: {res.observed_new} observed, {res.marked_posted} marked posted")
    typer.echo(f"  unmatched (not ShopSteward drafts): {res.unmatched}")
    for line in res.skipped:
        typer.secho(f"  skipped {line}", fg="yellow")


@ops_app.command("mark-posted")
def mark_posted_cmd(
    action_id: Annotated[
        str,
        typer.Argument(help="action_id from `ops brief`'s PINS TO POST/CAPTIONS TO POST/DONE"),
    ],
    posted_at: Annotated[
        str | None,
        typer.Option(
            "--posted-at",
            help=(
                "When this was ACTUALLY posted (ISO date/datetime), if different from now -- "
                "e.g. batching Friday's marks for a Monday post. Must fall between the draft's "
                "own drafted_at and now."
            ),
        ),
    ] = None,
) -> None:
    """Record that a drafted Pinterest pin (`social.pin_drafted`) OR a
    drafted caption (`social.caption_drafted`, T5+E5 2026-08-25) was posted
    by hand: appends `social.pin_posted`/`social.caption_posted`, which
    drops it out of `ops brief`'s PINS TO POST/CAPTIONS TO POST copy-paste
    queue. Pure record-keeping -- no governor, no adapter, no Pinterest/
    Meta call of any kind. Safe to run twice (a repeat call for an
    already-posted action_id is a no-op). Tries the pin channel first, then
    the caption channel -- an action_id that resolves to neither surfaces
    the PIN resolver's error message (unchanged CLI behavior for an
    action_id that predates T5 or matches nothing at all).

    `--posted-at` (E4, 2026-08-25): defaults to wall-clock now, exactly as
    before. An out-of-range value (future, or before the draft's own
    drafted_at) is rejected with a nonzero exit and appends nothing."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.ops.capabilities import caption_draft, pinterest_post
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    mark_pin_posted = pinterest_post.mark_posted
    mark_caption_posted = caption_draft.mark_posted

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        try:
            appended = mark_pin_posted(conn, DEFAULT_USER_ID, action_id, posted_at=posted_at)
        except ValueError as pin_exc:
            try:
                appended = mark_caption_posted(
                    conn, DEFAULT_USER_ID, action_id, posted_at=posted_at
                )
            except ValueError:
                typer.secho(f"mark-posted failed: {pin_exc}", fg="red")
                raise typer.Exit(code=1) from pin_exc
        if appended:
            typer.echo(f"marked posted: {action_id}")
        else:
            typer.echo(f"already marked posted: {action_id}")
    finally:
        conn.close()


class EditListingError(Exception):
    """A direct edit that was refused or did not execute; the message is
    operator/agent-facing (cleanly printed by `ops edit-listing`)."""


def run_direct_edit(
    conn: "sqlite3.Connection",
    user_id: int,
    cfg: "OpsConfig",
    seo_cap: "Capability",
    listing_id: int,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
    reason: str | None = None,
    live_autonomy: bool = False,
    today: "date | None" = None,
) -> tuple[str, dict[str, tuple[object, object]]]:
    """`ops edit-listing`'s core, separated from typer so it can be tested
    against a seeded fake adapter. Builds a `listing.seo_edit` action that
    bypasses ONLY `_eligible()` (seo_edit.build_direct_action) and runs it
    through the normal `runner.run()` path, so the governor, tiers, events and
    undo all apply. Returns (action_id, {field: (old, new)}); raises
    EditListingError (appending nothing where possible) otherwise.

    Without `live_autonomy` this refuses BEFORE any event is appended
    (runner.LIVE_GATED_CAPABILITIES: a fresh fake has no listing state), so
    no orphan pending proposal is left behind."""
    from shopsteward.core.events import read_all
    from shopsteward.pipeline.ops.capabilities.seo_edit import build_direct_action
    from shopsteward.pipeline.ops.runner import _action_status, _live_gate_blocks, run

    if _live_gate_blocks(seo_cap.key, live_autonomy):
        raise EditListingError(
            "live autonomy gate not set -- refusing to edit against the fake adapter "
            "(retry with --live-autonomy once ready)"
        )
    try:
        action, changes = build_direct_action(
            conn,
            user_id,
            cfg,
            listing_id,
            title=title,
            tags=tags,
            description=description,
            reason=reason,
            today=today,
        )
    except ValueError as exc:
        raise EditListingError(str(exc)) from exc

    run(
        conn,
        user_id,
        cfg,
        [seo_cap],
        dry_run=False,
        today=today,
        proposals=[action],
        live_autonomy=live_autonomy,
    )
    status = _action_status(conn, user_id, action.action_id)
    if status == "executed":
        return action.action_id, changes
    if not cfg.autonomy.enabled:
        why = "autonomy.enabled is false"
    elif status == "refused":
        refusals = [
            e.payload.get("reason")
            for e in read_all(conn, "action.refused")
            if e.user_id == user_id and e.payload.get("action_id") == action.action_id
        ]
        why = f"refused: {refusals[-1] if refusals else 'unknown'}"
    elif status == "proposed":
        why = "left pending -- effective tier is not AUTO (run `ops config apply`?)"
    else:
        why = f"not executed (state: {status or 'never proposed'})"
    raise EditListingError(f"edit not executed ({action.action_id}): {why}")


@ops_app.command("edit-listing")
def edit_listing_cmd(
    listing_id: Annotated[int, typer.Argument(help="Etsy listing_id to edit")],
    title: Annotated[str | None, typer.Option("--title", help="New title")] = None,
    tags: Annotated[
        str | None, typer.Option("--tags", help="New tags, comma-separated: 'a,b,c'")
    ] = None,
    description_file: Annotated[
        Path | None, typer.Option("--description-file", help="File holding the new description")
    ] = None,
    reason: Annotated[str | None, typer.Option("--reason", help="Why (kept in the event)")] = None,
    live_autonomy: Annotated[
        bool,
        typer.Option(
            "--live-autonomy/--no-live-autonomy", help="Permit real (non-fake) capability writes"
        ),
    ] = False,
) -> None:
    """Directly edit a listing's title/tags/description as a `listing.seo_edit`
    action. No approval (operator decision 2026-11-22), but the whole chassis
    still applies: governor (cooldown, caps), events, undo (`ops undo
    <action_id>`). Bypasses only seo_edit's eligibility signal. EXECUTES ->
    adapter-gated exactly like `ops approve`."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.live_gate import live_autonomy_error, live_autonomy_open
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.pipeline.ops.registry import REGISTRY
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_autonomy and not live_autonomy_open():
        typer.secho(live_autonomy_error(), fg="red")
        raise typer.Exit(code=1)

    tag_list = [t.strip() for t in tags.split(",")] if tags is not None else None
    description = description_file.read_text(encoding="utf-8") if description_file else None

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
            action_id, changes = run_direct_edit(
                conn,
                DEFAULT_USER_ID,
                cfg,
                REGISTRY["listing.seo_edit"],
                listing_id,
                title=title,
                tags=tag_list,
                description=description,
                reason=reason,
                live_autonomy=live_autonomy,
            )
        except EditListingError as exc:
            typer.secho(f"edit-listing failed: {exc}", fg="red")
            raise typer.Exit(code=1) from exc
        typer.echo(f"edited listing {listing_id}: action_id={action_id}")
        for field, (old, new) in changes.items():
            typer.echo(f"  {field}: {old!r} -> {new!r}")
        typer.echo(f"undo with: shopsteward ops undo {action_id} --live-autonomy")
    finally:
        conn.close()


@ops_app.command("replace-file")
def replace_file_cmd(
    listing_id: Annotated[int, typer.Argument(help="Etsy listing_id whose rank-1 file to replace")],
    file: Annotated[Path, typer.Option("--file", help="The re-exported full-frame JPEG")],
    reason: Annotated[str | None, typer.Option("--reason", help="Why (kept in the event)")] = None,
    live_autonomy: Annotated[
        bool,
        typer.Option(
            "--live-autonomy/--no-live-autonomy", help="Permit real (non-fake) capability writes"
        ),
    ] = False,
) -> None:
    """Replace a live digital listing's rank-1 file (`listing.file_replace`). Runs at
    AUTO when the old file is reproducible from its archived master (a real undo)
    and the new file is the same photo; otherwise it is recorded as a proposal
    held for `ops approve <action_id> --live-autonomy`. Reads Etsy's file list,
    so it is adapter-gated like every other live command."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild as rebuild_core
    from shopsteward.pipeline.live_gate import live_autonomy_error, live_autonomy_open
    from shopsteward.pipeline.ops import config as ops_config
    from shopsteward.pipeline.ops.projections import rebuild_ops
    from shopsteward.pipeline.ops.registry import REGISTRY
    from shopsteward.pipeline.ops.runner import run
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if not live_autonomy:
        typer.secho("replace-file needs --live-autonomy (it reads Etsy's file list)", fg="red")
        raise typer.Exit(code=1)
    if not live_autonomy_open():
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
        cap = REGISTRY["listing.file_replace"]
        try:
            action = cap.build_action(  # type: ignore[attr-defined]
                conn, DEFAULT_USER_ID, cfg, listing_id, file, reason=reason
            )
        except ValueError as exc:
            typer.secho(f"replace-file refused: {exc}", fg="red")
            raise typer.Exit(code=1) from exc
        report = run(
            conn,
            DEFAULT_USER_ID,
            cfg,
            [cap],
            proposals=[action],
            live_autonomy=live_autonomy,
        )
        if report.executed:
            typer.echo(f"replaced: {action.action_id} (undo: ops undo {action.action_id})")
        else:
            typer.echo(f"proposed: {action.action_id} ({action.reason})")
            typer.echo(f"execute with: shopsteward ops approve {action.action_id} --live-autonomy")
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
    from shopsteward.pipeline.ops.runner import LiveGateBlockedError, undo_action
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
            undo_action(
                conn,
                DEFAULT_USER_ID,
                action_id,
                list(REGISTRY.values()),
                live_autonomy=live_autonomy,
            )
        except LiveGateBlockedError:
            # Finding 2: same failure door as `ops approve` -- refuse BEFORE
            # calling cap.undo() against the fresh, empty fake, leaving the
            # executed action untouched (retry with --live-autonomy).
            typer.secho(
                "live autonomy gate not set -- refusing to undo against the fake "
                f"adapter; action left executed (unresolved): {action_id} "
                "(retry with --live-autonomy once ready)",
                fg="red",
            )
            raise typer.Exit(code=1) from None
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
