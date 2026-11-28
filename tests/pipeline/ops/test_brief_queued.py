"""NEEDS YOU vs QUEUED (next run): a pending proposal whose capability's
EFFECTIVE tier is AUTO/NOTIFY is not waiting on the operator -- the next run
drives it through the guards itself. The brief reads those guards (reprice
blackout/cooldown, gapfill pace, autorenew_on's sold-in-window check); it never
re-implements them and never appends an event."""

import json
from datetime import date, timedelta

import pytest

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.capabilities import gapfill
from shopsteward.pipeline.ops.capabilities.autorenew_on import ListingAutorenewOn
from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
from shopsteward.pipeline.ops.models import ProposedAction, Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, compute_action_id, register
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
AS_OF = date(2026, 12, 1)


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _insert(conn, type_: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (USER_ID, type_, json.dumps(payload), f"{AS_OF.isoformat()}T00:00:00.000000Z"),
    )
    conn.commit()


def _propose(conn, cfg, capability: str, target_id: str, **params) -> ProposedAction:
    action = ProposedAction(
        action_id=compute_action_id(capability, target_id, "h", "c", AS_OF.isoformat()),
        capability=capability,
        target_type="listing",
        target_id=target_id,
        tier=Tier.PROPOSE,  # minted before a tier lift
        reason=f"{capability} on {target_id}",
        inputs_hash="h",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(AS_OF + timedelta(days=14)).isoformat(),
        params=params,
    )
    _insert(conn, "action.proposed", action.model_dump())
    return action


def _built(tmp_path, *, blackout_until: str | None = None):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    cfg = cfg.model_copy(
        update={"reprice": cfg.reprice.model_copy(update={"blackout_until": blackout_until})}
    )
    return conn, cfg


def _brief(conn, cfg):
    rebuild_ops(conn)
    return generate_brief(conn, USER_ID, cfg, as_of=AS_OF)


def test_reprice_in_blackout_is_queued_and_blocked_not_needs_you(tmp_path):
    conn, cfg = _built(tmp_path, blackout_until="2026-12-03")
    register(ListingReprice(FakeEtsyWriteAdapter()))
    a = _propose(conn, cfg, "listing.reprice", "555", price_usd=9.0)

    brief = _brief(conn, cfg)

    assert brief.needs_you == []
    (q,) = brief.queued
    assert q.action_id == a.action_id
    assert not q.will_execute
    assert "blackout until 2026-12-03" in q.verdict
    text = render_text(brief)
    assert "NEEDS YOU (0)" in text
    assert "QUEUED (next run)" in text
    assert "BLOCKED: reprice blackout until 2026-12-03" in text


def test_reprice_outside_blackout_will_run(tmp_path):
    conn, cfg = _built(tmp_path, blackout_until=None)
    register(ListingReprice(FakeEtsyWriteAdapter()))
    _propose(conn, cfg, "listing.reprice", "555", price_usd=9.0)

    (q,) = _brief(conn, cfg).queued

    assert q.will_execute
    assert "WILL RUN" in render_text(_brief(conn, cfg))


def test_autorenew_on_without_a_recent_sale_names_the_sold_in_window_guard(tmp_path):
    conn, cfg = _built(tmp_path)
    register(ListingAutorenewOn(FakeEtsyWriteAdapter()))
    _propose(conn, cfg, "listing.autorenew_on", "777")

    brief = _brief(conn, cfg)

    assert brief.needs_you == []
    (q,) = brief.queued
    assert not q.will_execute
    assert "no sale in the last" in q.verdict


def test_gapfill_pace_refusal_is_named(tmp_path, monkeypatch):
    conn, cfg = _built(tmp_path)
    a = _propose(conn, cfg, "listing.gapfill_reprint", "901:acrylic")
    cap = gapfill.ListingGapfillReprint(print_file_host=None)  # type: ignore[arg-type]
    register(cap)
    monkeypatch.setattr(gapfill, "_candidates", lambda *_a, **_k: {"901:acrylic": a})
    monkeypatch.setattr(gapfill, "pace_exceeded", lambda *_a, **_k: True)

    (q,) = _brief(conn, cfg).queued

    assert not q.will_execute
    assert "gapfill weekly pace" in q.verdict


def test_gapfill_no_longer_buildable_says_it_will_be_closed(tmp_path, monkeypatch):
    conn, cfg = _built(tmp_path)
    _propose(conn, cfg, "listing.gapfill_reprint", "901:canvas_portrait")
    register(gapfill.ListingGapfillReprint(print_file_host=None))  # type: ignore[arg-type]
    monkeypatch.setattr(gapfill, "_candidates", lambda *_a, **_k: {})

    (q,) = _brief(conn, cfg).queued

    assert not q.will_execute
    assert "no longer eligible" in q.verdict


def test_propose_tier_capability_and_auto_hold_stay_in_needs_you(tmp_path):
    conn, cfg = _built(tmp_path)
    register(StubCapability(key="stub.propose", max_tier=Tier.PROPOSE))
    register(StubCapability(key="listing.file_replace", max_tier=Tier.AUTO))
    p = _propose(conn, cfg, "stub.propose", "t1")
    h = _propose(conn, cfg, "listing.file_replace", "888", auto_hold="no verified undo")

    brief = _brief(conn, cfg)

    assert {n.action_id for n in brief.needs_you} == {p.action_id, h.action_id}
    assert brief.queued == []


def test_unregistered_capability_stays_in_needs_you(tmp_path):
    conn, cfg = _built(tmp_path)
    a = _propose(conn, cfg, "listing.reprice", "555", price_usd=9.0)

    brief = _brief(conn, cfg)

    assert [n.action_id for n in brief.needs_you] == [a.action_id]
    assert brief.queued == []


def test_building_the_brief_appends_no_events(tmp_path):
    conn, cfg = _built(tmp_path, blackout_until=None)
    register(ListingReprice(FakeEtsyWriteAdapter()))
    _propose(conn, cfg, "listing.reprice", "555", price_usd=9.0)
    before = len(read_all(conn))

    _brief(conn, cfg)

    assert len(read_all(conn)) == before
