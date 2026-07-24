import time

import pytest

from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
from shopsteward.pipeline.live_gate import (
    live_copy_error,
    live_copy_open,
    live_etsy_write_error,
    live_etsy_write_open,
    live_vision_error,
    live_vision_open,
)

PROVIDER_KEYS = {"openrouter": "OPENROUTER_API_KEY", "gemini": "GEMINI_API_KEY"}


def _clear(monkeypatch) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_VISION", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.mark.parametrize("provider", ["openrouter", "gemini"])
def test_closed_when_flag_unset(monkeypatch, provider) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(PROVIDER_KEYS[provider], "some-key")
    assert live_vision_open(provider) is False


@pytest.mark.parametrize("provider", ["openrouter", "gemini"])
def test_open_when_flag_and_matching_key_set(monkeypatch, provider) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_VISION", "1")
    monkeypatch.setenv(PROVIDER_KEYS[provider], "some-key")
    assert live_vision_open(provider) is True


@pytest.mark.parametrize("provider", ["openrouter", "gemini"])
def test_closed_when_only_other_providers_key_set(monkeypatch, provider) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_VISION", "1")
    other = "gemini" if provider == "openrouter" else "openrouter"
    monkeypatch.setenv(PROVIDER_KEYS[other], "some-key")
    assert live_vision_open(provider) is False


@pytest.mark.parametrize("provider", ["openrouter", "gemini"])
def test_error_message_names_correct_env_var(provider) -> None:
    message = live_vision_error(provider)
    assert PROVIDER_KEYS[provider] in message
    other = "gemini" if provider == "openrouter" else "openrouter"
    assert PROVIDER_KEYS[other] not in message


def _clear_copy(monkeypatch) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_COPY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def test_live_copy_closed_when_flag_unset(monkeypatch) -> None:
    _clear_copy(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "some-key")
    assert live_copy_open() is False


def test_live_copy_closed_when_key_unset(monkeypatch) -> None:
    _clear_copy(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_COPY", "1")
    assert live_copy_open() is False


def test_live_copy_open_when_flag_and_key_set(monkeypatch) -> None:
    _clear_copy(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_COPY", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "some-key")
    assert live_copy_open() is True


def test_live_copy_error_names_env_vars() -> None:
    message = live_copy_error()
    assert "SHOPSTEWARD_LIVE_COPY" in message
    assert "OPENROUTER_API_KEY" in message


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


def _clear_etsy_write(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_WRITE", raising=False)
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tmp_path / "tokens.json"))


def test_live_etsy_write_closed_when_flag_unset(monkeypatch, tmp_path) -> None:
    _clear_etsy_write(monkeypatch, tmp_path)
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens())
    assert live_etsy_write_open() is False


def test_live_etsy_write_closed_when_no_tokens(monkeypatch, tmp_path) -> None:
    _clear_etsy_write(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_WRITE", "1")
    assert live_etsy_write_open() is False


def test_live_etsy_write_closed_when_scope_missing(monkeypatch, tmp_path) -> None:
    _clear_etsy_write(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_WRITE", "1")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r"]))
    assert live_etsy_write_open() is False


def test_live_etsy_write_closed_when_api_key_missing(monkeypatch, tmp_path) -> None:
    _clear_etsy_write(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_WRITE", "1")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r", "listings_w"]))
    assert live_etsy_write_open() is False


def test_live_etsy_write_open_when_flag_key_and_scope_present(monkeypatch, tmp_path) -> None:
    _clear_etsy_write(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_WRITE", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r", "listings_w"]))
    assert live_etsy_write_open() is True


def test_live_etsy_write_error_names_env_var_and_scope() -> None:
    message = live_etsy_write_error()
    assert "SHOPSTEWARD_LIVE_ETSY_WRITE" in message
    assert "listings_w" in message
