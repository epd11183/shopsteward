import pytest

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.adapters.printfile.live import LiveR2PrintFileHost
from shopsteward.pipeline.listings.pod.factory import build_print_file_host

_R2_VARS = (
    "CLOUDFLARE_R2_KEY",
    "CLOUDFLARE_R2_SECRET",
    "CLOUDFLARE_R2_ENDPOINT",
    "CLOUDFLARE_R2_BUCKET",
)


def _clear(monkeypatch) -> None:
    for var in _R2_VARS:
        monkeypatch.delenv(var, raising=False)


def test_default_is_the_fake_host():
    assert isinstance(build_print_file_host(live=False), FakePrintFileHost)


def test_live_without_credentials_raises(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(RuntimeError, match="CLOUDFLARE_R2_KEY"):
        build_print_file_host(live=True)


def test_live_with_all_credentials_returns_the_live_host(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_R2_KEY", "key")
    monkeypatch.setenv("CLOUDFLARE_R2_SECRET", "secret")
    monkeypatch.setenv("CLOUDFLARE_R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("CLOUDFLARE_R2_BUCKET", "prints")

    host = build_print_file_host(live=True)

    assert isinstance(host, LiveR2PrintFileHost)


def test_live_with_one_missing_credential_names_it(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_R2_KEY", "key")
    monkeypatch.setenv("CLOUDFLARE_R2_SECRET", "secret")
    monkeypatch.setenv("CLOUDFLARE_R2_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    # CLOUDFLARE_R2_BUCKET intentionally left unset
    with pytest.raises(RuntimeError, match="CLOUDFLARE_R2_BUCKET"):
        build_print_file_host(live=True)
