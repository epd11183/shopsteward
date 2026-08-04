from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from shopsteward.adapters.etsy.auth import EtsyTokens, EtsyTokenStore
from shopsteward.adapters.etsy.models import EtsyReceipt, EtsyShop
from shopsteward.cli import app

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures" / "etsy"


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "ingest" in result.output


def test_ingest_requires_mode(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", str(tmp_path)])
    assert result.exit_code != 0  # --mode is required; path exists so that's the only error


class _SpyReadAdapter:
    """Stands in for LiveEtsyAdapter: exposes only the three read methods
    (no create/update/publish/delete at all) and records every call so a
    test can assert the write surface is unreachable from `sync --live`."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_shop(self) -> EtsyShop:
        self.calls.append("get_shop")
        return EtsyShop(shop_id=1, shop_name="spy-shop")

    def list_listings(self) -> list[Any]:
        self.calls.append("list_listings")
        return []

    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]:
        self.calls.append("list_receipts")
        return []


def _sync_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "shopsteward.db"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_READ", raising=False)
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    monkeypatch.setenv("SHOPSTEWARD_ETSY_TOKENS", str(tmp_path / "tokens.json"))


def test_sync_refuses_when_neither_flag_passed(monkeypatch, tmp_path: Path) -> None:
    _sync_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code != 0
    assert "--fixtures or --live" in result.output


def test_sync_refuses_both_flags_together(monkeypatch, tmp_path: Path) -> None:
    _sync_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["sync", "--fixtures", str(FIXTURES), "--live"])
    assert result.exit_code != 0
    assert "not both" in result.output


def test_sync_live_without_env_var_refuses(monkeypatch, tmp_path: Path) -> None:
    _sync_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    EtsyTokenStore(tmp_path / "tokens.json").save(
        EtsyTokens(
            access_token="tok",
            access_expires_at=9999999999,
            refresh_token="refresh",
            shop_id=1,
            etsy_user_id=1,
            scopes=["listings_r"],
        )
    )
    result = runner.invoke(app, ["sync", "--live"])
    assert result.exit_code != 0
    assert "SHOPSTEWARD_LIVE_ETSY_READ" in result.output


def test_sync_env_var_without_live_flag_refuses(monkeypatch, tmp_path: Path) -> None:
    _sync_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_READ", "1")
    result = runner.invoke(app, ["sync"])
    assert result.exit_code != 0
    assert "--fixtures or --live" in result.output


def test_sync_live_with_flag_and_env_but_no_tokens_refuses(monkeypatch, tmp_path: Path) -> None:
    _sync_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SHOPSTEWARD_LIVE_ETSY_READ", "1")
    monkeypatch.setenv("ETSY_API_KEY", "keystr")
    # no tokens file written -> EtsyTokenStore.load() returns None
    result = runner.invoke(app, ["sync", "--live"])
    assert result.exit_code != 0
    assert "SHOPSTEWARD_LIVE_ETSY_READ" in result.output


def test_sync_fixtures_path_still_works(monkeypatch, tmp_path: Path) -> None:
    _sync_env(monkeypatch, tmp_path)
    result = runner.invoke(app, ["sync", "--fixtures", str(FIXTURES)])
    assert result.exit_code == 0, result.output
    assert "synced:" in result.output


def test_sync_live_gate_open_calls_only_read_methods(monkeypatch, tmp_path: Path) -> None:
    _sync_env(monkeypatch, tmp_path)
    spy = _SpyReadAdapter()
    monkeypatch.setattr("shopsteward.pipeline.live_gate.live_etsy_read_open", lambda: True)
    monkeypatch.setattr("shopsteward.cli._build_live_etsy_adapter", lambda: spy)

    result = runner.invoke(app, ["sync", "--live"])

    assert result.exit_code == 0, result.output
    assert spy.calls == ["get_shop", "list_listings", "list_receipts"]
    # LiveEtsyAdapter (and this spy standing in for it) carries no write
    # method at all -- the read sync path can never reach one.
    for write_method in (
        "create_draft_listing",
        "upload_listing_image",
        "upload_listing_file",
        "update_listing",
        "update_listing_price",
        "publish_listing",
        "delete_listing",
    ):
        assert not hasattr(spy, write_method)
