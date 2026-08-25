"""E11 -- `approve_action(..., live_autonomy=False)` must refuse BEFORE
calling `cap.execute()` for a capability in `LIVE_GATED_CAPABILITIES`,
leaving the proposal exactly as it was (still "proposed", never a terminal
`action.failed`/`action.executed`). `cli.py`'s `approve_cmd` is the one real
caller that ever passes `live_autonomy=False` (no `--live-autonomy`); every
other caller in this suite keeps the default `live_autonomy=True` (an
explicit test-mode opt-in, same precedent as every capability test file's
own pre-seeded fake).

Finding 2 (guardrail review, 2026-08-25): E11 only closed this door for
`approve_action()` -- `ops run --no-dry-run` and `ops undo` reach the SAME
fresh, empty fake identically. `run()`/`undo_action()` now check the same
`LIVE_GATED_CAPABILITIES` gate (via the shared `_live_gate_blocks()`
predicate) before executing/undoing."""

from datetime import UTC, datetime

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.runner import (
    LIVE_GATED_CAPABILITIES,
    LiveGateBlockedError,
    approve_action,
    run,
    undo_action,
)
from tests.pipeline.ops.stub_capability import StubCapability

USER_ID = 1
TODAY = datetime.now(UTC).date()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _cfg(**autonomy_overrides):
    cfg = ops_config.load_ops_config()
    cfg.autonomy.enabled = True
    for k, v in autonomy_overrides.items():
        setattr(cfg.autonomy, k, v)
    return cfg


def test_live_gated_capabilities_matches_the_real_registered_keys():
    """Finding 5: derive the expected set from the REAL registration path
    (`cli._register_autorenew`) rather than a hand-written literal -- a
    literal copy of LIVE_GATED_CAPABILITIES's own contents would go green
    even if a new adapter-backed capability forgot to register itself in
    that set. `hasattr(cap, "_adapter")` distinguishes an Etsy-write-adapter
    -backed capability (autorenew_off/on, reprice, seo_edit, deactivate,
    renew) from one with no adapter at all (tune_threshold, caption_draft,
    pinterest_post) or an offline, non-Etsy adapter (gapfill_reprint's
    `_host`)."""
    from shopsteward.pipeline.ops.cli import _register_autorenew
    from shopsteward.pipeline.ops.registry import REGISTRY

    REGISTRY.clear()
    try:
        _register_autorenew(False)
        adapter_backed = {cap.key for cap in REGISTRY.values() if hasattr(cap, "_adapter")}
    finally:
        REGISTRY.clear()

    assert adapter_backed == LIVE_GATED_CAPABILITIES


def test_without_live_autonomy_refuses_before_execute_and_leaves_proposal_pending(conn):
    cfg = _cfg()
    cap = StubCapability(key="listing.seo_edit", targets={"111": {"on": True}})
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    report = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=False
    )

    assert report.live_gate_blocked is True
    assert report.executed == 0
    assert report.failed == 0
    assert cap.execute_calls == []  # never touched the (fake) adapter
    assert read_all(conn, "action.executed") == []
    assert read_all(conn, "action.failed") == []
    assert read_all(conn, "action.approved") == []
    proposed = [e for e in read_all(conn, "action.proposed") if e.payload["action_id"] == action_id]
    assert len(proposed) == 1  # untouched, still pending


def test_a_non_live_gated_capability_is_unaffected_by_live_autonomy_false(conn):
    cfg = _cfg()
    cap = StubCapability(key="social.caption_draft", targets={"111": {"on": True}})
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    report = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=False
    )

    assert report.live_gate_blocked is False
    assert report.executed == 1
    assert cap.execute_calls == ["111"]


def test_gate_blocked_proposal_is_recoverable_with_live_autonomy_true(conn):
    """The whole point of E11: a gate-blocked approval must NOT be
    permanently resolved -- a later retry with live_autonomy=True (the
    operator setting --live-autonomy once ready) still executes it."""
    cfg = _cfg()
    cap = StubCapability(key="listing.renew", targets={"111": {"on": True}})
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    blocked = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=False
    )
    assert blocked.live_gate_blocked is True

    retried = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )

    assert retried.executed == 1
    assert cap.execute_calls == ["111"]


# --- finding 2: the same door via `ops run --no-dry-run` and `ops undo` ----


def _promote(conn, capability: str, to_tier: Tier) -> None:
    """Directly seeds a `capability.promoted` event -- standing in for the
    ladder actually earning it -- so a StubCapability reaches an
    AUTO_EXECUTE tier (NOTIFY/AUTO) inside `run()` without a separate
    operator approval step."""
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="capability.promoted",
            payload={
                "capability": capability,
                "from_tier": int(Tier.PROPOSE),
                "to_tier": int(to_tier),
                "trigger": "ladder",
            },
        ),
    )


def test_run_no_dry_run_without_the_gate_does_not_terminalize_a_live_gated_proposal(conn):
    """`ops run --no-dry-run` reaches the SAME fresh, empty fake as
    `approve_action()` for an AUTO/NOTIFY-tier live-gated capability --
    without `live_autonomy=True` (run()'s own default) it must refuse
    before executing, leaving the proposal "proposed", still approvable."""
    cfg = _cfg()
    cap = StubCapability(
        key="listing.seo_edit", max_tier=Tier.NOTIFY, targets={"111": {"on": True}}
    )
    _promote(conn, "listing.seo_edit", Tier.NOTIFY)

    report = run(conn, USER_ID, cfg, [cap], dry_run=False, today=TODAY)

    assert report.live_gate_blocked is True
    assert report.executed == 0
    assert report.failed == 0
    assert cap.execute_calls == []
    assert read_all(conn, "action.executed") == []
    assert read_all(conn, "action.failed") == []
    assert read_all(conn, "action.approved") == []
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]

    # still approvable later, once the operator sets --live-autonomy.
    retried = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert retried.executed == 1
    assert cap.execute_calls == ["111"]


def test_undo_without_the_gate_refuses_before_undo_and_leaves_the_action_executed(conn):
    """`ops undo` reaches the SAME fresh, empty fake -- without
    `live_autonomy=True` it must raise `LiveGateBlockedError` BEFORE calling
    `cap.undo()`, leaving the already-executed action untouched (no
    `action.undone`, no demotion, no double `cap.undo()` call later)."""
    cfg = _cfg()
    cap = StubCapability(key="listing.deactivate", targets={"111": {"on": True}})
    run(conn, USER_ID, cfg, [cap], today=TODAY)
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True)
    assert cap.execute_calls == ["111"]

    with pytest.raises(LiveGateBlockedError):
        undo_action(conn, USER_ID, action_id, [cap], live_autonomy=False)

    assert cap.undo_calls == []
    assert read_all(conn, "action.undone") == []

    # still undoable later, once the operator sets --live-autonomy.
    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
    assert cap.undo_calls == ["111"]
