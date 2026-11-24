"""`shopsteward listings publish`: read-back gate then gate3.publish. Fakes only."""

import pytest
from typer.testing import CliRunner

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.vision.fake import FakeCopyChecker
from shopsteward.adapters.vision.interface import CopyCheckResult, CopyCheckVerdict
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.listings.readback import ReadbackResult

from .test_gate3 import _build_one_pushed_draft

runner = CliRunner()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    conn = connect(db)
    migrate(conn)
    adapter = FakeEtsyWriteAdapter()
    draft_id = _build_one_pushed_draft(conn, tmp_path, adapter)
    for name in ("live_etsy_write_open", "live_copy_open"):
        monkeypatch.setattr(f"shopsteward.pipeline.live_gate.{name}", lambda: True)
    monkeypatch.setattr("shopsteward.pipeline.live_gate.live_vision_open", lambda provider: True)
    monkeypatch.setattr(
        "shopsteward.pipeline.listings.push.build_etsy_write_adapter", lambda *, live: adapter
    )
    monkeypatch.setattr(
        "shopsteward.pipeline.vision_factory.build_copy_checker",
        lambda profile, live: FakeCopyChecker(
            [CopyCheckResult(verdict=CopyCheckVerdict(ok=False, problems=["claims snow"]))]
        ),
    )
    return conn, adapter, draft_id


def _gate(monkeypatch, ok: bool, problems: list[str] | None = None) -> None:
    monkeypatch.setattr(
        "shopsteward.pipeline.listings.readback.run_readback",
        lambda conn, user_id, draft_id, **kw: ReadbackResult(
            draft_id=draft_id, ok=ok, problems=problems or []
        ),
    )


def _publishes(adapter) -> int:
    return [c for c, _ in adapter.calls].count("publish_listing")


def test_gate_pass_publishes_and_emits_event(env, monkeypatch):
    conn, adapter, draft_id = env
    _gate(monkeypatch, True)
    result = runner.invoke(app, ["listings", "publish", "--draft-id", draft_id, "--live"])
    assert result.exit_code == 0, result.output
    assert _publishes(adapter) == 1
    (ev,) = read_all(conn, "gate3.published")
    assert ev.payload["draft_id"] == draft_id


def test_gate_fail_does_not_publish_and_exits_nonzero(env):
    conn, adapter, draft_id = env  # real run_readback + failing fake checker
    result = runner.invoke(app, ["listings", "publish", "--draft-id", draft_id, "--live"])
    assert result.exit_code == 1
    assert "claims snow" in result.output
    assert _publishes(adapter) == 0
    assert read_all(conn, "gate3.published") == []


def test_dry_run_never_publishes(env, monkeypatch):
    conn, adapter, draft_id = env
    _gate(monkeypatch, True)
    result = runner.invoke(app, ["listings", "publish", "--draft-id", draft_id])
    assert result.exit_code == 0, result.output
    assert "Nothing published" in result.output
    assert _publishes(adapter) == 0
    assert read_all(conn, "gate3.published") == []


def test_listing_id_resolves_to_draft(env, monkeypatch):
    conn, adapter, draft_id = env
    _gate(monkeypatch, True)
    row = conn.execute(
        "SELECT etsy_listing_id FROM proj_listing_drafts WHERE draft_id=?", (draft_id,)
    ).fetchone()
    result = runner.invoke(
        app, ["listings", "publish", "--listing-id", row["etsy_listing_id"], "--live"]
    )
    assert result.exit_code == 0, result.output
    assert _publishes(adapter) == 1
    missing = runner.invoke(app, ["listings", "publish", "--listing-id", "999999"])
    assert missing.exit_code == 1


def test_closed_gate_refuses(env, monkeypatch):
    conn, adapter, draft_id = env
    _gate(monkeypatch, True)
    monkeypatch.setattr("shopsteward.pipeline.live_gate.live_copy_open", lambda: False)
    result = runner.invoke(app, ["listings", "publish", "--draft-id", draft_id, "--live"])
    assert result.exit_code == 1
    assert _publishes(adapter) == 0


def test_already_published_refused(env, monkeypatch):
    conn, adapter, draft_id = env
    _gate(monkeypatch, True)
    runner.invoke(app, ["listings", "publish", "--draft-id", draft_id, "--live"])
    again = runner.invoke(app, ["listings", "publish", "--draft-id", draft_id, "--live"])
    assert again.exit_code == 1
    assert "not publishable" in again.output
    assert _publishes(adapter) == 1
