import time

import pytest

from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
from shopsteward.pipeline.live_gate import (
    live_copy_error,
    live_copy_open,
    live_etsy_read_error,
    live_etsy_read_open,
    live_etsy_write_error,
    live_etsy_write_open,
    live_gelato_error,
    live_gelato_open,
    live_planner_error,
    live_planner_open,
    live_printfile_error,
    live_printfile_open,
    live_vision_error,
    live_vision_open,
)

_R2_VARS = (
    "CLOUDFLARE_R2_KEY",
    "CLOUDFLARE_R2_SECRET",
    "CLOUDFLARE_R2_ENDPOINT",
    "CLOUDFLARE_R2_BUCKET",
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


def _clear_planner(monkeypatch) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_PLANNER", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def test_live_planner_closed_when_flag_unset(monkeypatch) -> None:
    _clear_planner(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "some-key")
    assert live_planner_open() is False


def test_live_planner_closed_when_key_unset(monkeypatch) -> None:
    _clear_planner(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_PLANNER", "1")
    assert live_planner_open() is False


def test_live_planner_open_when_flag_and_key_set(monkeypatch) -> None:
    _clear_planner(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_PLANNER", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "some-key")
    assert live_planner_open() is True


def test_live_planner_error_names_env_vars_and_flag() -> None:
    message = live_planner_error()
    assert "SHOPSTEWARD_LIVE_PLANNER" in message
    assert "OPENROUTER_API_KEY" in message
    assert "--narrate" in message


def _clear_gelato(monkeypatch) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_GELATO", raising=False)
    monkeypatch.delenv("GELATO_API_KEY", raising=False)
    monkeypatch.delenv("GELATO_STORE_ID", raising=False)


def test_live_gelato_closed_when_flag_unset(monkeypatch) -> None:
    _clear_gelato(monkeypatch)
    monkeypatch.setenv("GELATO_API_KEY", "some-key")
    monkeypatch.setenv("GELATO_STORE_ID", "store-1")
    assert live_gelato_open() is False


def test_live_gelato_closed_when_key_unset(monkeypatch) -> None:
    _clear_gelato(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_GELATO", "1")
    monkeypatch.setenv("GELATO_STORE_ID", "store-1")
    assert live_gelato_open() is False


def test_live_gelato_closed_when_store_id_unset(monkeypatch) -> None:
    _clear_gelato(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_GELATO", "1")
    monkeypatch.setenv("GELATO_API_KEY", "some-key")
    assert live_gelato_open() is False


def test_live_gelato_open_when_flag_key_and_store_id_set(monkeypatch) -> None:
    _clear_gelato(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_GELATO", "1")
    monkeypatch.setenv("GELATO_API_KEY", "some-key")
    monkeypatch.setenv("GELATO_STORE_ID", "store-1")
    assert live_gelato_open() is True


def test_live_gelato_error_names_flag_key_store_and_cli_flag() -> None:
    message = live_gelato_error()
    assert "SHOPSTEWARD_LIVE_GELATO" in message
    assert "GELATO_API_KEY" in message
    assert "GELATO_STORE_ID" in message
    assert "--live-gelato" in message


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


def _clear_etsy_read(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_READ", raising=False)
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tmp_path / "tokens.json"))


def test_live_etsy_read_closed_when_flag_unset(monkeypatch, tmp_path) -> None:
    _clear_etsy_read(monkeypatch, tmp_path)
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens())
    assert live_etsy_read_open() is False


def test_live_etsy_read_closed_when_no_tokens(monkeypatch, tmp_path) -> None:
    _clear_etsy_read(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_READ", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    assert live_etsy_read_open() is False


def test_live_etsy_read_closed_when_scope_missing(monkeypatch, tmp_path) -> None:
    _clear_etsy_read(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_READ", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_w"]))
    assert live_etsy_read_open() is False


def test_live_etsy_read_closed_when_api_key_missing(monkeypatch, tmp_path) -> None:
    _clear_etsy_read(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_READ", "1")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r"]))
    assert live_etsy_read_open() is False


def test_live_etsy_read_open_when_flag_key_and_scope_present(monkeypatch, tmp_path) -> None:
    _clear_etsy_read(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_READ", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(_tokens(scopes=["listings_r"]))
    assert live_etsy_read_open() is True


def test_live_etsy_read_error_names_env_var_and_scope() -> None:
    message = live_etsy_read_error()
    assert "SHOPSTEWARD_LIVE_ETSY_READ" in message
    assert "listings_r" in message


def _clear_printfile(monkeypatch) -> None:
    monkeypatch.delenv("SHOPSTEWARD_LIVE_PRINTFILE", raising=False)
    for var in _R2_VARS:
        monkeypatch.delenv(var, raising=False)


def test_live_printfile_closed_when_flag_unset(monkeypatch) -> None:
    _clear_printfile(monkeypatch)
    for var in _R2_VARS:
        monkeypatch.setenv(var, "x")
    assert live_printfile_open() is False


@pytest.mark.parametrize("missing_var", _R2_VARS)
def test_live_printfile_closed_when_any_credential_missing(monkeypatch, missing_var) -> None:
    _clear_printfile(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_PRINTFILE", "1")
    for var in _R2_VARS:
        if var != missing_var:
            monkeypatch.setenv(var, "x")
    assert live_printfile_open() is False


def test_live_printfile_open_when_flag_and_all_credentials_set(monkeypatch) -> None:
    _clear_printfile(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_PRINTFILE", "1")
    for var in _R2_VARS:
        monkeypatch.setenv(var, "x")
    assert live_printfile_open() is True


def test_live_printfile_never_opens_on_the_management_token_alone(monkeypatch) -> None:
    # design §17 Q1a: CLOUDFLARE_R2_TOKEN is not a substitute for the four
    # object credentials -- setting only it must never open the gate.
    _clear_printfile(monkeypatch)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_PRINTFILE", "1")
    monkeypatch.setenv("CLOUDFLARE_R2_TOKEN", "management-credential")
    assert live_printfile_open() is False


def test_live_printfile_error_names_all_four_env_vars() -> None:
    message = live_printfile_error()
    assert "SHOPSTEWARD_LIVE_PRINTFILE" in message
    for var in _R2_VARS:
        assert var in message
    assert "CLOUDFLARE_R2_TOKEN" not in message
