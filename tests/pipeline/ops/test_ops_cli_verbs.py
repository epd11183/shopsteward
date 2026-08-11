"""`ops approve`/`ops reject`/`ops undo` (M8a spec §8 PR3): the CLI verbs
wiring runner.approve_action/.reject_action/.undo_action. Everything runs
against a FakeEtsyWriteAdapter -- `build_etsy_write_adapter` is monkeypatched
to hand back a pre-seeded fake instance so `ListingAutorenewOff.execute()`/
`.undo()` have a listing to write to, with zero network at any point."""

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

import shopsteward.pipeline.listings.push as push_mod
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY
from tests.pipeline.ops.helpers import seed_listing_observed_on

runner = CliRunner()

USER_ID = 1
TODAY = datetime.now(UTC).date()
LISTING_ID = 501


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _seed_config(conn, **autonomy_overrides) -> None:
    """A pre-seeded 'default' opsconfig -- the CLI's own ops_config.seed()
    call is a no-op once a config of that name already exists, so this is
    what get_ops_config() will actually return to the CLI. Only
    weekly_catalog_pct_cap needs to move here (default 0.10 would portfolio-
    cap-refuse a single proposal once rebuild_core() populates proj_listings
    with 1 active listing)."""
    cfg = ops_config.load_ops_config()
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="opsconfig.seeded",
            payload={
                "name": cfg.name,
                "config": cfg.model_dump(by_alias=True),
                "source": "defaults",
            },
        ),
    )


def _seed_dead_listing(conn, listing_id: int) -> None:
    title = f"Listing {listing_id}"
    for days_ago in (200, 100, 1):
        seed_listing_observed_on(
            conn,
            listing_id=listing_id,
            title=title,
            day=TODAY - timedelta(days=days_ago),
            views=50,
            state="active",
            should_auto_renew=True,
        )


def _propose_autorenew_off(conn, listing_id: int) -> str:
    """Directly appends one action.proposed event via the real capability's
    propose() (contract: 'append an action.proposed directly' is an
    accepted seeding strategy) -- independent of whatever adapter/instance
    the CLI itself constructs later."""
    cfg = ops_config.get_ops_config(conn, USER_ID)
    cap = ListingAutorenewOff(FakeEtsyWriteAdapter())
    actions = cap.propose(conn, USER_ID, cfg)
    (action,) = [a for a in actions if a.target_id == str(listing_id)]
    append(conn, Event(user_id=USER_ID, type="action.proposed", payload=action.model_dump()))
    return action.action_id


def _seeded_db(tmp_path, monkeypatch, *, autonomy_overrides: dict | None = None):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_AUTONOMY", raising=False)
    monkeypatch.delenv("ETSY_API_KEY", raising=False)

    conn = connect(db)
    migrate(conn)
    _seed_config(conn, **(autonomy_overrides or {"weekly_catalog_pct_cap": 1.0}))
    _seed_dead_listing(conn, LISTING_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    action_id = _propose_autorenew_off(conn, LISTING_ID)
    conn.close()
    return db, action_id


def _patch_fake_adapter(monkeypatch, fake: FakeEtsyWriteAdapter) -> None:
    def _builder(*, live: bool) -> FakeEtsyWriteAdapter:
        assert live is False, "the default/test path must never request a live adapter"
        return fake

    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", _builder)


def test_approve_executes_and_the_fake_reflects_the_write(tmp_path, monkeypatch):
    db, action_id = _seeded_db(tmp_path, monkeypatch)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_ID, should_auto_renew=True, state="active")
    _patch_fake_adapter(monkeypatch, fake)

    result = runner.invoke(app, ["ops", "approve", action_id])

    assert result.exit_code == 0, result.output
    assert "executed" in result.output
    assert fake.listings[LISTING_ID]["should_auto_renew"] is False

    conn = connect(db)
    executed = [e for e in read_all(conn, "action.executed") if e.payload["action_id"] == action_id]
    assert len(executed) == 1
    approved = [e for e in read_all(conn, "action.approved") if e.payload["action_id"] == action_id]
    assert approved[0].payload["by"] == "operator"


def test_reject_records_rejection_and_demotes_no_adapter_needed(tmp_path, monkeypatch):
    db, action_id = _seeded_db(tmp_path, monkeypatch)

    # No monkeypatch of build_etsy_write_adapter at all -- reject must never
    # even import it.
    result = runner.invoke(app, ["ops", "reject", action_id])

    assert result.exit_code == 0, result.output
    assert "rejected" in result.output
    conn = connect(db)
    rejected = [e for e in read_all(conn, "action.rejected") if e.payload["action_id"] == action_id]
    assert len(rejected) == 1
    demoted = [e for e in read_all(conn, "capability.demoted")]
    assert len(demoted) == 1
    assert demoted[0].payload["capability"] == "listing.autorenew_off"


def test_undo_restores_the_fake_after_an_approve(tmp_path, monkeypatch):
    db, action_id = _seeded_db(tmp_path, monkeypatch)
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LISTING_ID, should_auto_renew=True, state="active")
    _patch_fake_adapter(monkeypatch, fake)

    approved = runner.invoke(app, ["ops", "approve", action_id])
    assert approved.exit_code == 0, approved.output
    assert fake.listings[LISTING_ID]["should_auto_renew"] is False

    result = runner.invoke(app, ["ops", "undo", action_id])

    assert result.exit_code == 0, result.output
    assert "restored" in result.output
    assert fake.listings[LISTING_ID]["should_auto_renew"] is True

    conn = connect(db)
    undone = [e for e in read_all(conn, "action.undone") if e.payload["action_id"] == action_id]
    assert len(undone) == 1
    assert undone[0].payload["restored_to"] == {"should_auto_renew": True}


def test_approve_unknown_action_id_exits_nonzero_without_a_traceback(tmp_path, monkeypatch):
    db, _ = _seeded_db(tmp_path, monkeypatch)
    fake = FakeEtsyWriteAdapter()
    _patch_fake_adapter(monkeypatch, fake)

    result = runner.invoke(app, ["ops", "approve", "not-a-real-action-id"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_approve_with_live_autonomy_and_no_gate_refuses_and_never_builds_an_adapter(
    tmp_path, monkeypatch
):
    _, action_id = _seeded_db(tmp_path, monkeypatch)

    def _builder(*, live: bool) -> FakeEtsyWriteAdapter:
        raise AssertionError("build_etsy_write_adapter must not be called when the gate is closed")

    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", _builder)

    result = runner.invoke(app, ["ops", "approve", action_id, "--live-autonomy"])

    assert result.exit_code == 1
    assert "SHOPSTEWARD_LIVE_AUTONOMY" in result.output


def test_undo_with_live_autonomy_and_no_gate_refuses_and_never_builds_an_adapter(
    tmp_path, monkeypatch
):
    _, action_id = _seeded_db(tmp_path, monkeypatch)

    def _builder(*, live: bool) -> FakeEtsyWriteAdapter:
        raise AssertionError("build_etsy_write_adapter must not be called when the gate is closed")

    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", _builder)

    result = runner.invoke(app, ["ops", "undo", action_id, "--live-autonomy"])

    assert result.exit_code == 1
    assert "SHOPSTEWARD_LIVE_AUTONOMY" in result.output
