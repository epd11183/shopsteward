"""Runner gaps (2026-11-23): (1) a PROPOSE-era pending proposal must execute
once config lifts its capability to AUTO; (2) at most one executed action per
listing per run, deactivate winning."""

from datetime import date, timedelta

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import action_rows
from shopsteward.pipeline.ops.registry import StaleTargetError
from shopsteward.pipeline.ops.runner import run
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
DAY1 = date(2026, 1, 1)
SEO = "listing.seo_edit"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _cfg(floor: dict[str, int] | None = None):
    cfg = ops_config.load_ops_config()
    cfg.autonomy.enabled = True
    cfg.autonomy.tier_floor = dict(floor or {})
    return cfg


def _states(conn) -> dict[str, str]:
    return {r["target_id"] + "/" + r["capability"]: r["state"] for r in action_rows(conn)}


def _pending_stale_cap(conn) -> StubCapability:
    """A cap whose proposal was made while PROPOSE, and which no longer
    re-proposes (planner-only), so only the stale path can reach it."""
    cap = StubCapability(key=SEO, max_tier=Tier.AUTO, day=DAY1, target_type="listing")
    first = run(conn, USER_ID, _cfg(), [cap], today=DAY1, live_autonomy=True)
    assert first.proposed == 1 and first.executed == 0
    cap.propose = lambda *a: []  # type: ignore[method-assign]
    return cap


def test_stale_pending_executes_after_floor_lifts_to_auto(conn):
    cap = _pending_stale_cap(conn)
    report = run(
        conn,
        USER_ID,
        _cfg({SEO: int(Tier.AUTO)}),
        [cap],
        today=DAY1 + timedelta(days=1),
        live_autonomy=True,
    )
    assert report.executed == 1
    assert cap.execute_calls == ["t-1"]


def test_stale_pending_does_not_execute_on_dry_run(conn):
    cap = _pending_stale_cap(conn)
    report = run(
        conn,
        USER_ID,
        _cfg({SEO: int(Tier.AUTO)}),
        [cap],
        today=DAY1 + timedelta(days=1),
        dry_run=True,
        live_autonomy=True,
    )
    assert report.executed == 0
    assert cap.execute_calls == []


def test_stale_pending_not_executed_while_tier_is_propose(conn):
    cap = _pending_stale_cap(conn)
    report = run(conn, USER_ID, _cfg(), [cap], today=DAY1 + timedelta(days=1), live_autonomy=True)
    assert report.executed == 0
    assert cap.execute_calls == []


def test_expired_pending_does_not_execute(conn):
    cap = _pending_stale_cap(conn)
    cfg = _cfg({SEO: int(Tier.AUTO)})
    late = DAY1 + timedelta(days=cfg.autonomy.proposal_ttl_days + 1)
    report = run(conn, USER_ID, cfg, [cap], today=late, live_autonomy=True)
    assert report.executed == 0
    assert report.expired == 1
    assert cap.execute_calls == []


def test_revalidation_stale_pending_is_not_executed(conn):
    cap = _pending_stale_cap(conn)

    def _stale(*a):
        raise StaleTargetError("listing changed")

    cap.execute = _stale  # type: ignore[method-assign]
    report = run(
        conn,
        USER_ID,
        _cfg({SEO: int(Tier.AUTO)}),
        [cap],
        today=DAY1 + timedelta(days=1),
        live_autonomy=True,
    )
    assert report.executed == 0
    assert report.failed == 1
    assert cap.store["t-1"] == {"on": True}  # nothing was applied


def _pair(reprice_targets, deactivate_targets):
    reprice = StubCapability(
        key="listing.reprice",
        max_tier=Tier.AUTO,
        day=DAY1,
        target_type="listing",
        targets={t: {"on": True} for t in reprice_targets},
    )
    deactivate = StubCapability(
        key="listing.deactivate",
        max_tier=Tier.AUTO,
        day=DAY1,
        target_type="listing",
        targets={t: {"on": True} for t in deactivate_targets},
    )
    return reprice, deactivate


_AUTO_FLOOR = {"listing.reprice": 0, "listing.deactivate": 0}


def test_same_listing_deactivate_wins_and_reprice_is_refused(conn):
    reprice, deactivate = _pair(["4464177860"], ["4464177860"])
    # reprice listed FIRST: ordering must still let deactivate win.
    report = run(
        conn, USER_ID, _cfg(_AUTO_FLOOR), [reprice, deactivate], today=DAY1, live_autonomy=True
    )
    assert report.executed == 1
    assert deactivate.execute_calls == ["4464177860"]
    assert reprice.execute_calls == []
    refused = [e for e in read_all(conn, "action.refused")]
    assert [e.payload["reason"] for e in refused] == ["listing_conflict"]


def test_different_listings_are_unaffected(conn):
    reprice, deactivate = _pair(["111"], ["222"])
    report = run(
        conn, USER_ID, _cfg(_AUTO_FLOOR), [reprice, deactivate], today=DAY1, live_autonomy=True
    )
    assert report.executed == 2
    assert report.refused == 0
