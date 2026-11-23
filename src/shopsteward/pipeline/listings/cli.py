"""`shopsteward listings` sub-app: build local drafts + print status counts.
No gate3 CLI (UI is the decision surface, mirroring Gate 1) -- mockups/cli.py
convention. `archive_app` (registered as `shopsteward archive` at the root
CLI) is the source-photo-match backfill (design: source-photo-match):
operator-invoked, dry-run by default, never autonomous."""

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from shopsteward.adapters.etsy.live import LiveEtsyAdapter

listings_app = typer.Typer(no_args_is_help=True, help="Digital-direct Etsy listing drafts.")
config_app = typer.Typer(no_args_is_help=True, help="Stored listing config.")
listings_app.add_typer(config_app, name="config")
archive_app = typer.Typer(no_args_is_help=True, help="Local archive / source-photo-match backfill.")


@listings_app.command("build")
def build(
    photo_id: Annotated[str | None, typer.Option("--photo-id", help="Limit to one photo")] = None,
    force: Annotated[bool, typer.Option("--force", help="Rebuild even if idempotent")] = False,
    live_copy: Annotated[
        bool, typer.Option("--live-copy", help="Call the real OpenRouter copy API")
    ] = False,
    live_etsy_write: Annotated[
        bool, typer.Option("--live-etsy-write", help="Push drafts to the real Etsy API")
    ] = False,
) -> None:
    """Build local listing drafts for eligible landing files with a
    completed mockup set (run `shopsteward mockups run` first), then push
    every fully-built draft to Etsy (state=draft; Fake adapter by default).
    --force never re-pushes an already-pushed draft."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.live_gate import (
        live_copy_error,
        live_copy_open,
        live_etsy_write_error,
        live_etsy_write_open,
    )
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_copy and not live_copy_open():
        typer.secho(live_copy_error(), fg="red")
        raise typer.Exit(code=1)
    if live_etsy_write and not live_etsy_write_open():
        typer.secho(live_etsy_write_error(), fg="red")
        raise typer.Exit(code=1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        result = build_drafts(
            conn,
            DEFAULT_USER_ID,
            photo_id=photo_id,
            force=force,
            live_copy=live_copy,
            live_etsy_write=live_etsy_write,
        )
        typer.echo(f"listings build: {result.model_dump()}")
    finally:
        conn.close()


@listings_app.command("push")
def push(
    live_etsy_write: Annotated[
        bool, typer.Option("--live-etsy-write", help="Push drafts to the real Etsy API")
    ] = False,
) -> None:
    """Push already-built listing drafts to Etsy without rebuilding
    copy/price/images (`listings build` already does this automatically --
    use this to retry pushing after fixing something, e.g. re-authing with
    the listings_w scope). Idempotent: a draft that already has an Etsy
    listing id (pushed, or partially pushed then push_failed) is left alone
    -- re-running, with or without --force, never re-pushes it and never
    risks a duplicate Etsy listing."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import config as listing_config
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter, push_drafts
    from shopsteward.pipeline.live_gate import live_etsy_write_error, live_etsy_write_open
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if live_etsy_write and not live_etsy_write_open():
        typer.secho(live_etsy_write_error(), fg="red")
        raise typer.Exit(code=1)

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        listing_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        cfg = listing_config.get_config(conn, DEFAULT_USER_ID)
        adapter = build_etsy_write_adapter(live=live_etsy_write)
        result = push_drafts(conn, DEFAULT_USER_ID, cfg, adapter)
        typer.echo(f"listings push: {result.model_dump()}")
    finally:
        conn.close()


@listings_app.command("status")
def status() -> None:
    """Print listing draft counts by state."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_listings(conn)

        state_counts = {
            row["state"]: row["n"]
            for row in conn.execute(
                "SELECT state, COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=? "
                "GROUP BY state",
                (DEFAULT_USER_ID,),
            ).fetchall()
        }
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=?", (DEFAULT_USER_ID,)
        ).fetchone()["n"]

        typer.echo(f"Listing drafts: {total}")
        for state, count in sorted(state_counts.items()):
            typer.echo(f"  {state}: {count}")
    finally:
        conn.close()


@listings_app.command("reset")
def reset_cmd(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    apply_: Annotated[
        bool, typer.Option("--apply", help="Write reset events (default: dry-run)")
    ] = False,
    include_pushed: Annotated[
        bool,
        typer.Option("--include-pushed", help="Allow resetting pushed/POD-linked drafts too"),
    ] = False,
    confirm_listing_id: Annotated[
        list[str] | None,
        typer.Option("--confirm-listing-id", help="One per external id being reset"),
    ] = None,
    keep_landing: Annotated[
        bool,
        typer.Option("--keep-landing", help="Don't reset/re-scan the matching landing rows"),
    ] = False,
    reason: Annotated[
        str, typer.Option("--reason", help="Recorded on every event for forensics")
    ] = "operator_reset",
    format_: Annotated[
        list[str] | None,
        typer.Option(
            "--format",
            help="Limit the plan to only these format values (e.g. acrylic, poster, "
            "canvas, canvas_portrait, digital_download). Repeatable. Omitted: all "
            "formats in the folder, unchanged from today.",
        ),
    ] = None,
) -> None:
    """Winners-batch reset: un-poisons listing drafts built from files in
    `folder` (e.g. a fake dry-run push that left a fully-built draft with a
    fake etsy_listing_id) so `listings build` / `listings push` can run
    again for them. Dry-run by default: prints the plan and writes nothing.
    A draft in state=published or adopted (draft_id starting "adopted-") is
    always refused, no override. A draft already pushed/POD-linked (a
    non-NULL etsy_listing_id or provider_product_id) additionally needs
    --include-pushed plus one --confirm-listing-id per external id, matching
    EXACTLY the set this run would reset. --format (repeatable) narrows the
    plan to only rows whose format matches -- e.g. resetting fake POD-format
    drafts (acrylic/poster/canvas/canvas_portrait) for a folder that also has
    real, correctly-pushed digital_download listings, without ever putting
    those digital rows in the plan (and therefore never being forced to
    confirm/reset them). Note: unless --keep-landing, the
    landing re-observe step (scan_landing) scans the WHOLE folder, not just
    the reset files -- any other new file sitting in it gets ingested too,
    same as a normal `listings build` run would."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline import tuning
    from shopsteward.pipeline.config import TUNING_PROFILE_PATH
    from shopsteward.pipeline.listings.reset import ResetIncomplete, apply_reset, plan_reset
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        formats = set(format_) if format_ else None
        plan = plan_reset(
            conn, DEFAULT_USER_ID, folder, include_pushed=include_pushed, formats=formats
        )

        typer.echo(f"folder: {folder}")
        typer.echo(f"matched draft rows: {len(plan)}")
        typer.echo(
            f"{'draft_id':<14} {'landing_file_id':<16} {'state':<12} {'format':<16} "
            f"{'etsy_id':<10} {'pod_status':<12} verdict"
        )
        verdict_label = {
            "reset": "reset",
            "needs_confirmation": "needs --include-pushed",
            "refused_published": "REFUSED (published)",
            "refused_adopted": "REFUSED (use archive adopt-local --revoke)",
        }
        needs_confirmation_ids: set[str] = set()
        for row in plan:
            short_draft = row.draft_id[:12]
            short_landing = (row.landing_file_id or "-")[:14]
            typer.echo(
                f"{short_draft:<14} {short_landing:<16} {row.state:<12} "
                f"{row.format or '-':<16} {row.etsy_listing_id or '-':<10} "
                f"{row.pod_status or '-':<12} {verdict_label.get(row.verdict, row.verdict)}"
            )
            if row.verdict in ("reset", "needs_confirmation"):
                if row.etsy_listing_id is not None:
                    needs_confirmation_ids.add(row.etsy_listing_id)
                if row.provider_product_id is not None:
                    needs_confirmation_ids.add(row.provider_product_id)

        if needs_confirmation_ids:
            typer.echo(f"external ids requiring confirmation: {sorted(needs_confirmation_ids)}")

        if not apply_:
            hint = "--apply"
            if needs_confirmation_ids:
                confirm_flags = " ".join(
                    f"--confirm-listing-id {i}" for i in sorted(needs_confirmation_ids)
                )
                hint = f"--apply --include-pushed {confirm_flags}"
            typer.echo(f"Dry-run: nothing written. Re-run with {hint} to write.")
            return

        supplied_ids = set(confirm_listing_id or [])
        if needs_confirmation_ids != supplied_ids:
            missing = needs_confirmation_ids - supplied_ids
            extra = supplied_ids - needs_confirmation_ids
            typer.secho(
                f"--confirm-listing-id set mismatch. missing={sorted(missing)} "
                f"extra={sorted(extra)}",
                fg="red",
            )
            raise typer.Exit(code=1)

        if not keep_landing:
            # A follow-up scan_landing() inside apply_reset needs a seeded
            # tuning profile for landing.allowed_formats/min_long_edge_px --
            # only seeded here, on the write path, so a plain dry-run never
            # writes the tuningprofile.seeded event (design §5: dry-run
            # writes nothing).
            tuning.seed(conn, DEFAULT_USER_ID, TUNING_PROFILE_PATH)

        try:
            report = apply_reset(
                conn,
                DEFAULT_USER_ID,
                plan,
                folder=folder,
                reason=reason,
                keep_landing=keep_landing,
            )
        except ResetIncomplete as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(code=1) from exc
        typer.echo(f"listings reset: {report.model_dump()}")
    finally:
        conn.close()


def _fail(msg: str) -> typer.Exit:
    typer.secho(msg, fg="red")
    return typer.Exit(code=1)


@config_app.command("sync-defaults")
def config_sync_defaults() -> None:
    """Add top-level keys present in config/defaults/listing.json but missing
    from the stored config (never overwrites existing values)."""
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import config as listing_config
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        listing_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        added = listing_config.sync_defaults(conn, DEFAULT_USER_ID)
        typer.echo(f"added keys: {added}" if added else "nothing missing; stored config unchanged")
    finally:
        conn.close()


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Dotted path of an existing key")],
    value: Annotated[str, typer.Argument(help="JSON value: a quoted string, 5, null, {...}")],
) -> None:
    """Set one stored config value; the whole config is re-validated first."""
    import json

    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import config as listing_config
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise _fail(f"value is not valid JSON: {exc}") from exc
    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        listing_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        before = listing_config.config_hash(listing_config.get_config(conn, DEFAULT_USER_ID))
        try:
            new_cfg = listing_config.set_value(conn, DEFAULT_USER_ID, key, parsed)
        except ValueError as exc:
            raise _fail(str(exc)) from exc
        typer.echo(f"set {key} = {json.dumps(parsed)}")
        if listing_config.config_hash(new_cfg) != before:
            typer.secho(
                "warning: this changes the config hash; unpushed drafts will be re-drafted "
                "on the next build and only the newest draft per photo will be pushed; "
                "photos already on Etsy are skipped.",
                fg="yellow",
            )
    finally:
        conn.close()


def _resolve_landing_file(
    conn: sqlite3.Connection, landing_file_id: str | None, listing_id: int | None
) -> str:
    from shopsteward.pipeline.listings.source_assets import resolve_source
    from shopsteward.settings import DEFAULT_USER_ID

    if (landing_file_id is None) == (listing_id is None):
        raise _fail("Give exactly one of <landing_file_id> or --listing-id.")
    if listing_id is not None:
        ref = resolve_source(conn, DEFAULT_USER_ID, listing_id)
        if ref is None or ref.landing_file_id is None:
            raise _fail(f"No landing file linked to listing {listing_id} (adopt it first).")
        return ref.landing_file_id
    assert landing_file_id is not None
    return landing_file_id


def _landing_path(conn: sqlite3.Connection, landing_file_id: str) -> str:
    from shopsteward.settings import DEFAULT_USER_ID

    row = conn.execute(
        "SELECT path FROM proj_landing_files WHERE user_id=? AND file_id=?",
        (DEFAULT_USER_ID, landing_file_id),
    ).fetchone()
    if row is None:
        raise _fail(f"Unknown landing file {landing_file_id}.")
    return row["path"]


@listings_app.command("crop-focus")
def crop_focus_cmd(
    landing_file_id: Annotated[str | None, typer.Argument()] = None,
    listing_id: Annotated[int | None, typer.Option("--listing-id")] = None,
    x: Annotated[float | None, typer.Option("--x", help="0..1 from the left")] = None,
    y: Annotated[float | None, typer.Option("--y", help="0..1 from the top")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Reset to centred")] = False,
) -> None:
    """Set (or --clear) the operator crop focal point for a landing file."""
    from pydantic import ValidationError

    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import crop_focus
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.projections import rebuild_pipeline
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    has_xy = x is not None and y is not None
    if clear == has_xy or (x is None) != (y is None):
        raise _fail("Pass both --x and --y, or --clear.")
    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_pipeline(conn)
        rebuild_listings(conn)
        file_id = _resolve_landing_file(conn, landing_file_id, listing_id)
        _landing_path(conn, file_id)
        if clear:
            crop_focus.clear_crop_focus(conn, DEFAULT_USER_ID, file_id)
            typer.echo(f"crop focus cleared for {file_id[:12]}")
        else:
            assert x is not None and y is not None
            try:
                crop_focus.set_crop_focus(conn, DEFAULT_USER_ID, file_id, x, y)
            except ValidationError as exc:
                raise _fail(f"--x/--y must be within [0, 1]: {exc}") from exc
            typer.echo(f"crop focus for {file_id[:12]} = ({x}, {y})")
        # Design section 5: warn only; v1 never replaces already-uploaded files.
        pushed = conn.execute(
            "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND landing_file_id=? "
            "AND etsy_listing_id IS NOT NULL LIMIT 1",
            (DEFAULT_USER_ID, file_id),
        ).fetchone()
        if pushed is not None:
            typer.secho(
                "warning: a draft for this photo is already pushed; "
                "already-uploaded files are NOT replaced.",
                fg="yellow",
            )
        planned = conn.execute(
            "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND landing_file_id=? "
            "AND etsy_listing_id IS NULL AND files_plan_json IS NOT NULL LIMIT 1",
            (DEFAULT_USER_ID, file_id),
        ).fetchone()
        if planned is not None:
            typer.secho(
                "warning: an unpushed draft for this photo already has a file plan "
                "recorded with the old focus; run `listings build --force` to re-plan.",
                fg="yellow",
            )
    finally:
        conn.close()


@listings_app.command("crop-preview")
def crop_preview_cmd(
    out: Annotated[Path, typer.Option("--out", help="Output directory")],
    landing_file_id: Annotated[str | None, typer.Argument()] = None,
    listing_id: Annotated[int | None, typer.Option("--listing-id")] = None,
) -> None:
    """Write quick downscaled JPEG looks of each planned file (NOT the
    sellable files: no copyright/ICC/EXIF) and print the plan. Falls back to the
    default digital_files settings when the stored config has none (run
    `listings config sync-defaults` to align)."""
    from PIL import Image, ImageOps

    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import config as listing_config
    from shopsteward.pipeline.listings.crop_focus import get_crop_focus
    from shopsteward.pipeline.listings.digital_files import photo_dims, plan_files
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.projections import rebuild_pipeline
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        rebuild_pipeline(conn)
        listing_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        file_id = _resolve_landing_file(conn, landing_file_id, listing_id)
        path = _landing_path(conn, file_id)
        dcfg = (
            listing_config.get_config(conn, DEFAULT_USER_ID).digital_files
            or listing_config.load_listing_config().digital_files
        )
        if dcfg is None:
            raise _fail("No digital_files config in the stored config or the defaults.")
        focus = get_crop_focus(conn, DEFAULT_USER_ID, file_id)
        plan = plan_files(photo_dims(path), focus, dcfg)
    finally:
        conn.close()

    out.mkdir(parents=True, exist_ok=True)
    typer.echo(f"source {plan.source_dims} focus {plan.focus}")
    typer.echo(f"{'key':<8} {'rank':>4}  {'crop_box':<28} sizes")
    with Image.open(path) as src:
        base = ImageOps.exif_transpose(src).convert("RGB")
        for entry in plan.files:
            crop = base.crop(entry.crop_box)
            crop.thumbnail((1200, 1200))
            sizes = "-".join(entry.sizes) or "nosize"
            crop.save(out / f"{entry.key}_{sizes}.jpg", "JPEG", quality=85, comment=b"")
            box = str(entry.crop_box)
            typer.echo(f"{entry.key:<8} {entry.rank:>4}  {box:<28} {', '.join(entry.sizes)}")


@listings_app.command("backfill-files")
def backfill_files_cmd(
    listing_id: Annotated[int | None, typer.Option("--listing-id")] = None,
    live: Annotated[bool, typer.Option("--live", help="Upload (or --rollback) for real")] = False,
    rollback: Annotated[
        bool, typer.Option("--rollback", help="Delete files this backfill uploaded")
    ] = False,
) -> None:
    """Add the multi-ratio crops (ranks 2..N) to live digital listings. Dry-run
    by default (B0): reads Etsy (list files) but never uploads or deletes. Both
    modes need the live Etsy write gate (the file list is only on the write
    adapter). --live uploads; the existing rank-1 file is never touched.
    --rollback --live --listing-id N deletes only files this backfill uploaded."""
    from shopsteward.core import projections as core_projections
    from shopsteward.core.db import connect, migrate
    from shopsteward.mockups.projections import rebuild_mockups
    from shopsteward.pipeline.listings import backfill as bf
    from shopsteward.pipeline.listings import config as listing_config
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter
    from shopsteward.pipeline.live_gate import live_etsy_write_error, live_etsy_write_open
    from shopsteward.pipeline.projections import rebuild_pipeline
    from shopsteward.settings import DEFAULT_USER_ID, db_path

    if rollback and (not live or listing_id is None):
        raise _fail("--rollback needs --live and --listing-id.")
    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        listing_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        cfg = listing_config.get_config(conn, DEFAULT_USER_ID)
        if not rollback:  # rollback must work with digital_files null (design section 12)
            try:
                bf.check_preconditions(cfg, live=live)
            except bf.BackfillRefused as exc:
                raise _fail(str(exc)) from exc
        if not live_etsy_write_open():
            raise _fail(live_etsy_write_error())
        core_projections.rebuild(conn)
        rebuild_pipeline(conn)
        rebuild_mockups(conn)
        rebuild_listings(conn)
        adapter = build_etsy_write_adapter(live=True)

        if rollback:
            assert listing_id is not None
            r = bf.rollback(conn, DEFAULT_USER_ID, adapter, listing_id)
            typer.echo(f"{listing_id}: {r.status} {r.detail} (files removed: {r.changed})")
            if r.status in ("refused", "failed", "etsy_error"):
                raise typer.Exit(code=1)
            return

        if cfg.digital_files and not cfg.digital_files.copyright_holder:
            typer.secho("warning: copyright_holder is null; --live would refuse.", fg="yellow")
        report = bf.backfill(conn, DEFAULT_USER_ID, cfg, adapter, listing_id=listing_id, live=live)
        for r in report.results:
            typer.echo(f"{r.listing_id}: {r.status} {r.detail}".rstrip())
            if r.existing:
                typer.echo(f"    existing: {', '.join(r.existing)}")
            if r.to_upload:
                typer.echo(
                    f"    {'uploaded' if r.status == 'uploaded' else 'would upload'}: "
                    f"{', '.join(r.to_upload)}"
                )
            if r.undelivered:
                typer.echo(f"    copy claims undelivered sizes: {', '.join(r.undelivered)}")
            if r.panel:
                typer.echo(f"    what-you-get panel: {r.panel}")
        sources = {r.copy_source for r in report.results if r.copy_source}
        if "draft" in sources:
            typer.echo("note: description/tags from draft, may be stale (no synced snapshot).")
        if "synced" in sources:
            typer.echo("note: description/tags from the last `sync --live` snapshot; may be stale.")
        typer.echo(
            f"{len(report.results)} listings checked, {report.would_get} "
            f"{'got' if live else 'would get'} files, {report.loses_size} lose a claimed size; "
            f"other: {report.skip_counts()}"
        )
        if not live:
            typer.echo("Dry-run: nothing written. Re-run with --live to upload.")
        elif any(r.status in ("failed", "etsy_error", "refused") for r in report.results):
            raise typer.Exit(code=1)
    finally:
        conn.close()


def _build_live_etsy_read_adapter() -> "LiveEtsyAdapter":
    """Mirrors shopsteward.cli._build_live_etsy_adapter (that helper lives at
    the root CLI, which imports THIS module -- importing it back here would
    be circular, hence the small duplication, same reasoning push.py's own
    build_etsy_write_adapter already uses for the write side)."""
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


@archive_app.command("adopt-local")
def adopt_local_cmd(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    recursive: Annotated[
        bool, typer.Option("--recursive", help="Scan subfolders of `folder` too")
    ] = False,
    fixtures: Annotated[
        Path | None, typer.Option(help="Fixture dir (default source until live approved)")
    ] = None,
    live_etsy_read: Annotated[
        bool, typer.Option("--live-etsy-read", help="Read listing images from the real Etsy API")
    ] = False,
    apply_: Annotated[
        bool, typer.Option("--apply", help="Write archive + linkage events (default: dry-run)")
    ] = False,
    pin: Annotated[
        list[str] | None,
        typer.Option("--pin", help="LISTING_ID=FILE_PATH -- force one listing to adopt one file"),
    ] = None,
    revoke_ids: Annotated[
        list[int] | None,
        typer.Option("--revoke", help="Undo a prior adopt for this listing_id"),
    ] = None,
) -> None:
    """Match local archive photo files against real Etsy listing images via
    perceptual hash, then backfill the source-asset linkage for manual/
    pre-pipeline listings (source_assets.resolve_source() currently returns
    None for these). Dry-run by default: prints a
    `local file -> listing_id -> distance -> verdict` table and writes
    NOTHING. --apply re-runs the identical matching and archives + records
    the linkage for every `match` verdict (never `ambiguous`/`unmatched` --
    those need --pin)."""
    from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
    from shopsteward.core.db import connect, migrate
    from shopsteward.pipeline.listings import adopt, asset_store_config
    from shopsteward.pipeline.listings.projections import rebuild_listings
    from shopsteward.pipeline.live_gate import live_etsy_read_error, live_etsy_read_open
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

    pins: dict[int, str] = {}
    for raw in pin or []:
        listing_id_str, sep, file_path = raw.partition("=")
        if not sep or not file_path:
            typer.secho(f"Bad --pin value {raw!r}; expected LISTING_ID=FILE_PATH.", fg="red")
            raise typer.Exit(1)
        pins[int(listing_id_str)] = file_path

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    try:
        migrate(conn)
        asset_store_config.seed(conn, DEFAULT_USER_ID)
        rebuild_listings(conn)
        cfg = asset_store_config.get_asset_store_config(conn, DEFAULT_USER_ID)

        for listing_id in revoke_ids or []:
            if adopt.revoke(conn, DEFAULT_USER_ID, listing_id):
                typer.echo(f"revoked: listing {listing_id}")
            else:
                typer.echo(f"nothing to revoke: listing {listing_id} was never adopted")

        adapter = (
            _build_live_etsy_read_adapter() if live_etsy_read else FixtureEtsyAdapter(fixtures)
        )

        results = adopt.plan_matches(
            conn, DEFAULT_USER_ID, adapter, folder, recursive=recursive, cfg=cfg.match
        )

        typer.echo(f"{'local file':<60} {'listing_id':>10} {'distance':>8}  verdict")
        for r in results:
            distance_str = str(r.distance) if r.distance is not None else "-"
            local = r.local_path or "(none)"
            typer.echo(f"{local:<60} {r.listing_id:>10} {distance_str:>8}  {r.verdict}")
        for listing_id, file_path in pins.items():
            typer.echo(f"{file_path:<60} {listing_id:>10} {'-':>8}  pinned")

        if not apply_:
            typer.echo("Dry-run: nothing written. Pass --apply to adopt confirmed matches.")
            return

        report = adopt.apply_matches(conn, DEFAULT_USER_ID, cfg, results)
        for listing_id, file_path in pins.items():
            if adopt.adopt_one(
                conn,
                DEFAULT_USER_ID,
                cfg,
                listing_id=listing_id,
                local_path=file_path,
                distance=None,
                match_source="pin",
            ):
                report.pinned += 1
                report.adopted += 1
        typer.echo(f"adopt-local: {report.model_dump()}")
    finally:
        conn.close()
