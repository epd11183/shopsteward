"""`shopsteward pod config apply`: the rollback lever design §6/§15 and §14's
smoke-test prep assume exists (review fix-up C -- seed() alone has no path
to apply an operator edit to pod.json once it has been seeded once).

`shopsteward pod build [--dry-run]` (slice 2, design §13): builds POD drafts
through print_file_hosted -- provider create/poll/link/enrich is slice 3/4,
so there is no --live-pod flag yet. --dry-run runs the same selection +
pricing computation and prints it; it appends nothing and never reads or
hosts a print file (build_pod_drafts precedent: `listings build` in
listings/cli.py).

`shopsteward pod publish <format>` (design: pod-publish): batch-publishes
every currently-eligible (pod_status='enriched', has an etsy_listing_id, not
already published) draft of ONE named POD format -- "publish all my canvas
listings" as a single operator action, since a POD draft's real Etsy draft
listing already carries real copy+images by the time it reaches
pod_status='enriched' (Gelato create + pod/enrich.py). Routes every publish
through gate3.publish() (the sole call site for
EtsyWriteAdapter.publish_listing, PRD §13 decision 41) -- this command is a
batch caller of it, not a parallel publish path. Dry-run by default."""

import json
from typing import Annotated

import typer

_POD_FORMATS = ("acrylic", "poster", "canvas", "canvas_portrait")

pod_app = typer.Typer(no_args_is_help=True, help="POD (print-on-demand) physical listings.")
config_app = typer.Typer(no_args_is_help=True, help="pod.json config.")
pod_app.add_typer(config_app, name="config")
template_app = typer.Typer(no_args_is_help=True, help="Gelato template inspection (live read).")
pod_app.add_typer(template_app, name="template")


@template_app.command("show")
def template_show(
    template_id: Annotated[str, typer.Argument(help="Gelato templateId")],
) -> None:
    """Fetch a Gelato template and print each variant's templateVariantId +
    imagePlaceholder names, so the operator can fill pod.json's
    canvas/canvas_portrait variant_key/placeholder. Gated live read: needs
    SHOPSTEWARD_LIVE_GELATO=1 + GELATO_API_KEY (store_id is not required for
    the templates endpoint)."""
    import os

    from shopsteward.adapters.pod.live import LiveGelatoAdapter, format_template_variants
    from shopsteward.pipeline.live_gate import live_gelato_error, live_gelato_open

    if not live_gelato_open():
        typer.secho(live_gelato_error(), fg="red")
        raise typer.Exit(code=1)

    adapter = LiveGelatoAdapter(api_key=os.environ["GELATO_API_KEY"], store_id="")
    for line in format_template_variants(adapter.get_template(template_id)):
        typer.echo(line)


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


@pod_app.command("publish")
def publish(
    format_: Annotated[
        str,
        typer.Argument(metavar="FORMAT", help="acrylic, poster, canvas, or canvas_portrait"),
    ],
    apply_: Annotated[
        bool, typer.Option("--apply", help="Publish for real (default: dry-run)")
    ] = False,
    live_etsy_write: Annotated[
        bool, typer.Option("--live-etsy-write", help="Publish via the real Etsy API")
    ] = False,
) -> None:
    """Publish every currently-eligible (pod_status='enriched', has an
    etsy_listing_id, not already published) POD draft of ONE format --
    a real-money action (Etsy's per-listing fee applies). Dry-run by
    default: prints the count and a one-line-per-draft plan, writes
    nothing. --apply calls gate3.publish() (the sole call site for
    EtsyWriteAdapter.publish_listing, PRD §13 decision 41) for each
    eligible draft."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import gate3
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter
    from shopsteward.pipeline.live_gate import live_etsy_write_error, live_etsy_write_open
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if format_ not in _POD_FORMATS:
        typer.secho(f"format must be one of {_POD_FORMATS}, got {format_!r}.", fg="red")
        raise typer.Exit(code=1)

    if apply_ and not live_etsy_write:
        # --apply alone would silently run the real event-append path
        # against the FAKE adapter: gate3.publish() still appends a real,
        # permanent gate3.approved/gate3.published event and flips state
        # out of 'built' -- on a real POD draft this is unrecoverable
        # (undoing it means listingdraft.reset, which deletes the whole
        # provider_product_id/etsy_listing_id linkage). This is a real-
        # money action; require the operator to say so explicitly both ways.
        typer.secho(
            "--apply requires --live-etsy-write (a fake-adapter apply would still "
            "permanently mutate real draft state). Pass both, or omit --apply for "
            "a dry-run.",
            fg="red",
        )
        raise typer.Exit(code=1)

    if live_etsy_write and not live_etsy_write_open():
        typer.secho(live_etsy_write_error(), fg="red")
        raise typer.Exit(code=1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_listings(conn)
        rows = conn.execute(
            "SELECT draft_id, etsy_listing_id, title, variants_json FROM proj_listing_drafts "
            "WHERE user_id=? AND format=? AND pod_status='enriched' "
            "AND etsy_listing_id IS NOT NULL AND state != 'published' ORDER BY draft_id",
            (DEFAULT_USER_ID, format_),
        ).fetchall()

        typer.echo(f"format: {format_}")
        typer.echo(f"eligible drafts: {len(rows)}")
        typer.echo(f"{'draft_id':<14} {'etsy_id':<12} {'title':<40} price")
        for row in rows:
            title = (row["title"] or "-")[:40]
            # A POD draft's price lives per-variant in variants_json (the
            # digital-only `price` column is always NULL here) -- show the
            # lowest variant's retail_price as a representative figure.
            variants = json.loads(row["variants_json"] or "[]")
            retail_prices = [v["retail_price"] for v in variants if v.get("retail_price")]
            price = f"{min(retail_prices):.2f}+" if retail_prices else "-"
            typer.echo(
                f"{row['draft_id'][:12]:<14} {row['etsy_listing_id']:<12} {title:<40} {price}"
            )

        if not apply_:
            typer.echo("Dry-run: nothing written. Re-run with --apply to publish.")
            return

        adapter = build_etsy_write_adapter(live=live_etsy_write)
        published = 0
        failed = 0
        for row in rows:
            # gate3.publish() raises ValueError for an ineligible draft; this
            # query's predicate already matches its eligibility check, so it
            # shouldn't fire -- but one unexpected raise must not abort the
            # rest of a real-money batch with no summary printed.
            try:
                card = gate3.publish(conn, DEFAULT_USER_ID, row["draft_id"], adapter)
            except ValueError as exc:
                failed += 1
                typer.echo(f"  FAILED {row['draft_id'][:12]}: {exc}")
                continue
            if card.state == "published":
                published += 1
            else:
                failed += 1
                typer.echo(f"  FAILED {row['draft_id'][:12]}: {card.retry_error}")
        typer.echo(f"published: {published} failed: {failed} total: {len(rows)}")
    finally:
        conn.close()
