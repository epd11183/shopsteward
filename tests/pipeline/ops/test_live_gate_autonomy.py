import time

from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
from shopsteward.pipeline.live_gate import live_autonomy_error, live_autonomy_open


def _tokens(**overrides: object) -> EtsyTokens:
    base: dict = {
        "access_token": "tok",
        "access_expires_at": time.time() + 3600,
        "refresh_token": "refresh",
        "shop_id": 1,
        "etsy_user_id": 1,
        "scopes": ["listings_r", "listings_w"],
    }
    base.update(overrides)
    return EtsyTokens(**base)


def _clear(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_AUTONOMY", raising=False)
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tmp_path / "tokens.json"))


def test_closed_when_flag_unset(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch, tmp_path)
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens())
    assert live_autonomy_open() is False


def test_closed_when_no_tokens(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_AUTONOMY", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    assert live_autonomy_open() is False


def test_closed_when_scope_missing(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_AUTONOMY", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r"]))
    assert live_autonomy_open() is False


def test_closed_when_api_key_missing(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_AUTONOMY", "1")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r", "listings_w"]))
    assert live_autonomy_open() is False


def test_open_when_flag_key_and_scope_present(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_AUTONOMY", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r", "listings_w"]))
    assert live_autonomy_open() is True


def test_error_message_names_env_var_scope_and_flag() -> None:
    message = live_autonomy_error()
    assert "SHOPSTEWARD_LIVE_AUTONOMY" in message
    assert "listings_w" in message
    assert "--live-autonomy" in message
