import pytest

from shopsteward.pipeline.live_gate import (
    live_copy_error,
    live_copy_open,
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
