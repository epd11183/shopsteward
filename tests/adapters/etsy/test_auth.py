import base64
import hashlib
import json
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from shopsteward.adapters.etsy import auth as auth_mod
from shopsteward.adapters.etsy.auth import (
    DEFAULT_SCOPES,
    EtsyTokenAuth,
    EtsyTokens,
    EtsyTokenStore,
    build_auth_url,
    make_pkce,
    run_auth_flow,
)
from shopsteward.cli import app

runner = CliRunner()


def _tokens(**overrides: object) -> EtsyTokens:
    base: dict = {
        "access_token": "1234.accesstok",
        "access_expires_at": time.time() + 3600,
        "refresh_token": "refresh-secret",
        "shop_id": 999,
        "etsy_user_id": 1234,
        "scopes": ["listings_r"],
    }
    base.update(overrides)
    return EtsyTokens(**base)


def _drive_redirect(
    auth_url: str, *, state: str | None = None, code: str = "auth-code-123"
) -> None:
    """Spawn a thread that plays the browser: GET the redirect_uri baked
    into `auth_url` with the given code/state, after the server is up.

    The redirect_uri sent to Etsy reads "localhost" (must exact-match the
    registered callback), but the socket only listens on 127.0.0.1 — so the
    actual GET here is rewritten to 127.0.0.1. That's the test-harness
    "browser", not something Etsy needs to match.
    """
    parsed = httpx.URL(auth_url)
    resolved_state = state if state is not None else parsed.params["state"]
    redirect_uri = httpx.URL(parsed.params["redirect_uri"]).copy_with(host="127.0.0.1")

    def fire() -> None:
        time.sleep(0.05)
        with httpx.Client() as real_client:
            real_client.get(redirect_uri, params={"code": code, "state": resolved_state})

    threading.Thread(target=fire, daemon=True).start()


# --- token store -------------------------------------------------------


def test_store_round_trip(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    assert store.load() is None
    tokens = _tokens()
    store.save(tokens)
    assert store.load() == tokens


def test_store_load_missing_file_returns_none(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "nope.json")
    assert store.load() is None


def test_store_load_wrong_schema_raises(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"schema": "some.other/1"}), encoding="utf-8")
    store = EtsyTokenStore(path)
    with pytest.raises(ValueError):
        store.load()


# --- get_access_token ----------------------------------------------------


def test_get_access_token_returns_stored_when_fresh(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    store.save(_tokens(access_expires_at=time.time() + 3600))

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("should not hit the network when token is fresh")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    token = store.get_access_token("api-key", client=client)

    assert token == "1234.accesstok"
    assert calls == []


def test_get_access_token_refreshes_when_expired(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    store.save(_tokens(access_expires_at=time.time() - 10, refresh_token="old-refresh"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == auth_mod.TOKEN_URL
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert body["grant_type"] == "refresh_token"
        assert body["client_id"] == "api-key"
        assert body["refresh_token"] == "old-refresh"
        return httpx.Response(
            200,
            json={
                "access_token": "1234.newaccess",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fixed_now = time.time()
    token = store.get_access_token("api-key", client=client, now=lambda: fixed_now)

    assert token == "1234.newaccess"
    persisted = store.load()
    assert persisted is not None
    assert persisted.access_token == "1234.newaccess"
    assert persisted.refresh_token == "new-refresh"
    assert persisted.access_expires_at == pytest.approx(fixed_now + 3600)


def test_get_access_token_refresh_failure_does_not_leak_secret(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    store.save(_tokens(access_expires_at=time.time() - 10, refresh_token="super-secret-refresh"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "refresh_token": "super-secret-refresh"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError) as exc_info:
        store.get_access_token("api-key", client=client)

    message = str(exc_info.value)
    assert "super-secret-refresh" not in message
    assert "400" in message


def test_get_access_token_missing_file_raises_helpful_error(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    with pytest.raises(RuntimeError, match="shopsteward etsy auth"):
        store.get_access_token("api-key")


# --- PKCE + URL building ---------------------------------------------------


def test_make_pkce_challenge_matches_verifier() -> None:
    verifier, challenge = make_pkce()
    assert 43 <= len(verifier) <= 128
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert set(verifier) <= allowed

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected


def test_build_auth_url_contains_expected_params() -> None:
    # redirect_uri must read "localhost", not "127.0.0.1" — Etsy
    # exact-string-matches it against the registered callback.
    url = build_auth_url(
        "api-key", "http://localhost:8322/oauth/redirect", DEFAULT_SCOPES, "state123", "chal"
    )
    parsed = httpx.URL(url)
    params = dict(parsed.params)

    assert parsed.scheme == "https"
    assert parsed.host == "www.etsy.com"
    assert params["response_type"] == "code"
    assert params["client_id"] == "api-key"
    assert params["redirect_uri"] == "http://localhost:8322/oauth/redirect"
    assert params["scope"] == " ".join(DEFAULT_SCOPES)
    assert params["state"] == "state123"
    assert params["code_challenge"] == "chal"
    assert params["code_challenge_method"] == "S256"


# --- EtsyTokenAuth --------------------------------------------------------


def test_etsy_token_auth_attaches_bearer_header(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    store.save(_tokens(access_token="1234.freshtoken", access_expires_at=time.time() + 3600))

    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = httpx.Client(
        transport=httpx.MockTransport(handler), auth=EtsyTokenAuth(store, "api-key")
    )
    client.get("https://example.com/whatever")

    assert captured["auth"] == "Bearer 1234.freshtoken"


# --- run_auth_flow end-to-end ---------------------------------------------


def test_run_auth_flow_end_to_end(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == auth_mod.TOKEN_URL:
            body = dict(urllib.parse.parse_qsl(request.content.decode()))
            assert body["grant_type"] == "authorization_code"
            assert body["code"] == "auth-code-123"
            assert body["code_verifier"]
            assert body["redirect_uri"].startswith("http://localhost:")
            return httpx.Response(
                200,
                json={
                    "access_token": "5555.brandnew",
                    "refresh_token": "brandnew-refresh",
                    "expires_in": 3600,
                },
            )
        if "/shops" in str(request.url):
            return httpx.Response(200, json={"shop_id": 777})
        raise AssertionError(f"unexpected request {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def fake_open_browser(url: str) -> None:
        captured_urls.append(url)
        _drive_redirect(url)

    before = time.time()
    tokens = run_auth_flow(
        "api-key",
        port=0,
        timeout_s=5,
        client=client,
        open_browser=fake_open_browser,
        store=store,
    )
    after = time.time()

    assert tokens.access_token == "5555.brandnew"
    assert tokens.refresh_token == "brandnew-refresh"
    assert tokens.etsy_user_id == 5555
    assert tokens.shop_id == 777
    assert before + 3600 <= tokens.access_expires_at <= after + 3600
    assert store.load() == tokens
    assert captured_urls


def test_run_auth_flow_wrong_state_raises(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    def fake_open_browser(url: str) -> None:
        _drive_redirect(url, state="wrong-state")

    with pytest.raises(RuntimeError, match="state mismatch"):
        run_auth_flow(
            "api-key",
            port=0,
            timeout_s=5,
            client=client,
            open_browser=fake_open_browser,
            store=store,
        )


def test_run_auth_flow_denied_raises(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    def fake_open_browser(url: str) -> None:
        parsed = httpx.URL(url)
        redirect_uri = httpx.URL(parsed.params["redirect_uri"]).copy_with(host="127.0.0.1")

        def fire() -> None:
            time.sleep(0.05)
            with httpx.Client() as real_client:
                real_client.get(
                    redirect_uri,
                    params={"error": "access_denied", "state": parsed.params["state"]},
                )

        threading.Thread(target=fire, daemon=True).start()

    with pytest.raises(RuntimeError, match="authorization failed"):
        run_auth_flow(
            "api-key",
            port=0,
            timeout_s=5,
            client=client,
            open_browser=fake_open_browser,
            store=store,
        )


def test_run_auth_flow_env_shop_id_skips_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETSY_SHOP_ID", "42424242")
    store = EtsyTokenStore(tmp_path / "tokens.json")
    discovery_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/shops" in str(request.url):
            discovery_calls.append(request)
            return httpx.Response(200, json={"shop_id": 1})
        return httpx.Response(
            200,
            json={"access_token": "9999.tok", "refresh_token": "r", "expires_in": 3600},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def fake_open_browser(url: str) -> None:
        _drive_redirect(url)

    tokens = run_auth_flow(
        "api-key", port=0, timeout_s=5, client=client, open_browser=fake_open_browser, store=store
    )

    assert tokens.shop_id == 42424242
    assert discovery_calls == []


def test_run_auth_flow_malformed_access_token_raises_without_leaking(tmp_path: Path) -> None:
    store = EtsyTokenStore(tmp_path / "tokens.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == auth_mod.TOKEN_URL:
            return httpx.Response(
                200,
                json={
                    "access_token": "not-a-numeric-prefix-token",
                    "refresh_token": "r",
                    "expires_in": 3600,
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def fake_open_browser(url: str) -> None:
        _drive_redirect(url)

    with pytest.raises(RuntimeError) as exc_info:
        run_auth_flow(
            "api-key",
            port=0,
            timeout_s=5,
            client=client,
            open_browser=fake_open_browser,
            store=store,
        )

    message = str(exc_info.value)
    assert "not-a-numeric-prefix-token" not in message
    assert "unexpected Etsy access-token format" in message


# --- CLI -------------------------------------------------------------------


def test_etsy_status_no_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tmp_path / "tokens.json"))
    result = runner.invoke(app, ["etsy", "status"])
    assert result.exit_code == 0
    assert "No Etsy tokens" in result.output


def test_etsy_status_with_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens_path = tmp_path / "tokens.json"
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tokens_path))
    store = EtsyTokenStore(tokens_path)
    store.save(
        _tokens(
            shop_id=555,
            scopes=["listings_r", "shops_r"],
            access_expires_at=time.time() + 2600,
            access_token="1234.secretaccesstoken",
            refresh_token="secretrefreshtoken",
        )
    )

    result = runner.invoke(app, ["etsy", "status"])

    assert result.exit_code == 0
    assert "555" in result.output
    assert "listings_r" in result.output
    assert "expires in" in result.output
    assert "secretaccesstoken" not in result.output
    assert "secretrefreshtoken" not in result.output


def test_etsy_auth_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    result = runner.invoke(app, ["etsy", "auth"])
    assert result.exit_code == 1
    assert "ETSY_API_KEY" in result.output


def test_etsy_auth_rejects_write_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETSY_API_KEY", "key")
    result = runner.invoke(app, ["etsy", "auth", "--scopes", "listings_r listings_w"])
    assert result.exit_code == 1
    assert "write scopes arrive with M5 re-consent" in result.output
