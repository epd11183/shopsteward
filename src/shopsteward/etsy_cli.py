"""`shopsteward etsy` sub-app: PKCE OAuth flow and token status. Never
prints an access or refresh token — success output is shop_id/scopes only."""

import time
from typing import Annotated

import typer

from shopsteward.adapters.etsy.auth import DEFAULT_PORT, DEFAULT_SCOPES

etsy_app = typer.Typer(no_args_is_help=True, help="Etsy OAuth token management.")

_DEFAULT_SCOPES_STR = " ".join(DEFAULT_SCOPES)

# listings_w added for M5a (PRD §13 decision 41); feedback_r added
# 2026-08-24 for review-velocity reading (roadmap P4, operator-approved
# explicitly in-session per CLAUDE.md's rewritten governance -- see
# adapters/etsy/auth.py's DEFAULT_SCOPES for the matching change).
# Everything else stays denied.
_ALLOWED_SCOPES = {"listings_r", "listings_w", "transactions_r", "shops_r", "feedback_r"}


def _format_delta(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m"
    days = hours // 24
    return f"{days}d"


@etsy_app.command("auth")
def auth(
    port: Annotated[
        int,
        typer.Option(
            help="Local port for the OAuth redirect; must match the redirect URI "
            "registered with the Etsy app"
        ),
    ] = DEFAULT_PORT,
    timeout: Annotated[int, typer.Option(help="Seconds to wait for the browser redirect")] = 300,
    scopes: Annotated[
        str, typer.Option(help="Space-joined Etsy OAuth scopes")
    ] = _DEFAULT_SCOPES_STR,
) -> None:
    """Run the PKCE OAuth flow and store tokens locally."""
    import os

    from shopsteward.adapters.etsy.auth import EtsyTokenStore, run_auth_flow

    api_key = os.environ.get("ETSY_API_KEY")
    if not api_key:
        typer.secho("ETSY_API_KEY is not set in the environment.", fg="red")
        raise typer.Exit(1)
    if not os.environ.get("ETSY_SHARED_SECRET"):
        typer.secho(
            "ETSY_SHARED_SECRET is not set — Etsy v3 requires keystring:shared_secret "
            "in x-api-key; shop discovery and API reads will fail without it.",
            fg="yellow",
        )

    resolved_scopes = tuple(scopes.split())
    unknown_scopes = [s for s in resolved_scopes if s not in _ALLOWED_SCOPES]
    if unknown_scopes:
        typer.secho(
            f"Unsupported scope(s) {', '.join(unknown_scopes)}: only the "
            "operator-approved set is allowed (PRD decision 41).",
            fg="red",
        )
        raise typer.Exit(1)

    store = EtsyTokenStore()
    try:
        tokens = run_auth_flow(
            api_key,
            scopes=resolved_scopes,
            port=port,
            timeout_s=timeout,
            on_auth_url=lambda url: typer.echo(f"Authorize at: {url}"),
            store=store,
        )
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from None

    typer.echo("Authorized.")
    typer.echo(f"Shop: {tokens.shop_id if tokens.shop_id is not None else '(not discovered)'}")
    typer.echo(f"Scopes: {', '.join(tokens.scopes)}")
    typer.echo(f"Tokens stored at {store.path}")


@etsy_app.command("status")
def status() -> None:
    """Print Etsy auth status: tokens present, shop id, scopes, expiry."""
    from shopsteward.adapters.etsy.auth import EtsyTokenStore

    store = EtsyTokenStore()
    tokens = store.load()
    if tokens is None:
        typer.echo("No Etsy tokens found. Run `shopsteward etsy auth` first.")
        return

    typer.echo(f"Shop: {tokens.shop_id if tokens.shop_id is not None else '(not discovered)'}")
    typer.echo(f"Scopes: {', '.join(tokens.scopes)}")

    remaining = tokens.access_expires_at - time.time()
    if remaining > 0:
        typer.echo(f"Access token expires in {_format_delta(remaining)}")
    else:
        typer.echo("Access token expired — will auto-refresh on next use.")
