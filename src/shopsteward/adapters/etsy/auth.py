"""Etsy OAuth2 (PKCE) token acquisition and refresh. httpx only — no vendor
SDK (PRD §13 decision 35). Tokens live in a local JSON file (never the event
log); this module must never log, print, or raise an access/refresh token.

NOT wired into LiveEtsyAdapter yet — that happens only after the §8.4 smoke
test is operator-approved. `EtsyTokenAuth` is importable so that wiring is a
small follow-up, not a redesign.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from shopsteward.settings import etsy_tokens_path

TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
CONNECT_URL = "https://www.etsy.com/oauth/connect"
SHOPS_URL = "https://openapi.etsy.com/v3/application/users/{user_id}/shops"
DEFAULT_SCOPES = ("listings_r", "transactions_r", "shops_r")
DEFAULT_PORT = 8322
REDIRECT_PATH = "/oauth/redirect"

_SCHEMA = "shopsteward.etsytokens/1"


class EtsyTokens(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(default=_SCHEMA, alias="schema")
    access_token: str
    access_expires_at: float
    refresh_token: str
    shop_id: int | None
    etsy_user_id: int
    scopes: list[str]


class EtsyTokenStore:
    """Reads/writes the local Etsy token file and refreshes on demand.

    File writes are atomic (tmp file + os.replace), mirroring
    core/folderproto.py — readers never see a partial file.
    """

    def __init__(self, path: Path | None = None):
        self._path = path if path is not None else etsy_tokens_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> EtsyTokens | None:
        if not self._path.exists():
            return None
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        schema = raw.get("schema")
        if schema != _SCHEMA:
            raise ValueError(f"unexpected Etsy token file schema: {schema!r}")
        return EtsyTokens.model_validate(raw)

    def save(self, tokens: EtsyTokens) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        part_path = self._path.with_name(self._path.name + ".part")
        payload = tokens.model_dump(by_alias=True)
        with part_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part_path, self._path)

    def get_access_token(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
    ) -> str:
        """Return a valid access token, refreshing (and persisting the
        rotated refresh token) if the stored one expires within 60s."""
        tokens = self.load()
        if tokens is None:
            raise RuntimeError("No Etsy tokens on disk; run `shopsteward etsy auth` first.")

        if tokens.access_expires_at - now() > 60:
            return tokens.access_token

        owns_client = client is None
        http_client = client if client is not None else httpx.Client(timeout=30.0)
        try:
            resp = http_client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": api_key,
                    "refresh_token": tokens.refresh_token,
                },
            )
        finally:
            if owns_client:
                http_client.close()

        if resp.status_code != 200:
            error = _safe_error(resp)
            detail = f" ({error})" if error else ""
            raise RuntimeError(
                f"Etsy token refresh failed with HTTP {resp.status_code}{detail}; "
                "run `shopsteward etsy auth` again if this persists."
            )

        body = resp.json()
        new_tokens = EtsyTokens(
            access_token=body["access_token"],
            access_expires_at=now() + body["expires_in"],
            refresh_token=body.get("refresh_token", tokens.refresh_token),
            shop_id=tokens.shop_id,
            etsy_user_id=tokens.etsy_user_id,
            scopes=tokens.scopes,
        )
        self.save(new_tokens)
        return new_tokens.access_token


def _safe_error(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        error = body.get("error")
        return error if isinstance(error, str) else None
    return None


class EtsyTokenAuth(httpx.Auth):
    """httpx.Auth that pulls a fresh access token from the store per
    request. Not wired into LiveEtsyAdapter yet (see module docstring)."""

    def __init__(self, store: EtsyTokenStore, api_key: str):
        self._store = store
        self._api_key = api_key

    def auth_flow(self, request: httpx.Request):  # type: ignore[override]
        token = self._store.get_access_token(self._api_key)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def make_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) per RFC 7636 (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(
    api_key: str,
    redirect_uri: str,
    scopes: Sequence[str],
    state: str,
    challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{CONNECT_URL}?{urllib.parse.urlencode(params)}"


class _OAuthServer(http.server.HTTPServer):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.oauth_result: dict[str, str] | None = None


class _OAuthRedirectHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # silence default access logging to stderr

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = dict(urllib.parse.parse_qsl(parsed.query))
        server = self.server
        if isinstance(server, _OAuthServer):
            server.oauth_result = params

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body>Authorized \xe2\x80\x94 you can close this tab.</body></html>"
        )


def run_auth_flow(
    api_key: str,
    *,
    scopes: Sequence[str] = DEFAULT_SCOPES,
    port: int = DEFAULT_PORT,
    timeout_s: float = 300,
    client: httpx.Client | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    on_auth_url: Callable[[str], object] | None = None,
    store: EtsyTokenStore | None = None,
) -> EtsyTokens:
    """Run the PKCE authorization-code flow via a one-shot localhost server,
    exchange the code for tokens, discover the shop, and persist tokens.

    `port=0` lets the OS pick a free port (used by tests); the bound port is
    reflected in the redirect_uri sent to Etsy. `on_auth_url`, if given, is
    called with the consent URL before the browser opens, so a caller (e.g.
    the CLI) can print it without duplicating the PKCE challenge/state.
    """
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(32)

    # Socket binds to 127.0.0.1 (loopback), but the redirect_uri sent to Etsy
    # must read "localhost" — Etsy exact-string-matches it against the
    # callback URL registered with the app (e.g. http://localhost:8322/...).
    server = _OAuthServer(("127.0.0.1", port), _OAuthRedirectHandler)
    bound_port = server.server_address[1]
    redirect_uri = f"http://localhost:{bound_port}{REDIRECT_PATH}"
    auth_url = build_auth_url(api_key, redirect_uri, scopes, state, challenge)

    if on_auth_url is not None:
        on_auth_url(auth_url)
    open_browser(auth_url)

    deadline = time.monotonic() + timeout_s
    try:
        while server.oauth_result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Timed out waiting for the Etsy OAuth redirect. Open this URL to "
                    f"authorize manually: {auth_url}"
                )
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()

    result = server.oauth_result
    assert result is not None  # loop only exits with a result or an exception

    if "error" in result:
        raise RuntimeError(f"Etsy authorization failed: {result['error']}")
    if not secrets.compare_digest(result.get("state", ""), state):
        raise RuntimeError("OAuth state mismatch on the Etsy redirect; aborting.")
    code = result.get("code")
    if not code:
        raise RuntimeError("Etsy redirect did not include an authorization code.")

    owns_client = client is None
    http_client = client if client is not None else httpx.Client(timeout=30.0)
    try:
        token_resp = http_client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": api_key,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": verifier,
            },
        )
        if token_resp.status_code != 200:
            error = _safe_error(token_resp)
            detail = f" ({error})" if error else ""
            raise RuntimeError(
                f"Etsy token exchange failed with HTTP {token_resp.status_code}{detail}"
            )
        token_body = token_resp.json()
        access_token = token_body["access_token"]
        try:
            etsy_user_id = int(access_token.split(".", 1)[0])
        except ValueError:
            raise RuntimeError("unexpected Etsy access-token format") from None
        issued_at = time.time()
        access_expires_at = issued_at + token_body["expires_in"]

        shop_id = _resolve_shop_id(http_client, api_key, access_token, etsy_user_id)
    finally:
        if owns_client:
            http_client.close()

    tokens = EtsyTokens(
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=token_body["refresh_token"],
        shop_id=shop_id,
        etsy_user_id=etsy_user_id,
        scopes=list(scopes),
    )
    (store if store is not None else EtsyTokenStore()).save(tokens)
    return tokens


def _resolve_shop_id(
    client: httpx.Client, api_key: str, access_token: str, etsy_user_id: int
) -> int | None:
    env_shop_id = os.environ.get("ETSY_SHOP_ID")
    if env_shop_id:
        return int(env_shop_id)

    resp = client.get(
        SHOPS_URL.format(user_id=etsy_user_id),
        headers={"x-api-key": api_key, "authorization": f"Bearer {access_token}"},
    )
    if resp.status_code != 200:
        return None
    body = resp.json()
    if isinstance(body, dict) and "shop_id" in body:
        return body["shop_id"]
    if isinstance(body, dict) and body.get("results"):
        return body["results"][0].get("shop_id")
    return None
