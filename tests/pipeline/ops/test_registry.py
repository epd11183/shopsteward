import pytest

from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.registry import REGISTRY, compute_action_id, register
from tests.pipeline.ops.stub_capability import StubCapability


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def test_register_accepts_a_t2_ceiling_capability_with_no_undo():
    cap = StubCapability(key="stub.no_undo_t2", max_tier=Tier.PROPOSE, undoable=False)
    register(cap)  # T2 ceiling never auto-executes -- no undo required
    assert REGISTRY["stub.no_undo_t2"] is cap


def test_register_rejects_a_t1_ceiling_capability_with_no_undo():
    cap = StubCapability(key="stub.no_undo_t1", max_tier=Tier.NOTIFY, undoable=False)
    with pytest.raises(ValueError, match="undo"):
        register(cap)
    assert "stub.no_undo_t1" not in REGISTRY


def test_register_rejects_a_t0_ceiling_capability_with_no_undo():
    cap = StubCapability(key="stub.no_undo_t0", max_tier=Tier.AUTO, undoable=False)
    with pytest.raises(ValueError):
        register(cap)


def test_register_accepts_a_t1_ceiling_capability_with_a_working_undo():
    cap = StubCapability(key="stub.with_undo_t1", max_tier=Tier.NOTIFY, undoable=True)
    register(cap)
    assert REGISTRY["stub.with_undo_t1"] is cap


def test_compute_action_id_is_deterministic():
    a = compute_action_id("cap", "target", "hash", "cfghash", "2026-01-01")
    b = compute_action_id("cap", "target", "hash", "cfghash", "2026-01-01")
    assert a == b


def test_compute_action_id_differs_on_any_input():
    base = compute_action_id("cap", "target", "hash", "cfghash", "2026-01-01")
    assert compute_action_id("cap2", "target", "hash", "cfghash", "2026-01-01") != base
    assert compute_action_id("cap", "target2", "hash", "cfghash", "2026-01-01") != base
    assert compute_action_id("cap", "target", "hash2", "cfghash", "2026-01-01") != base
    assert compute_action_id("cap", "target", "hash", "cfghash2", "2026-01-01") != base
    assert compute_action_id("cap", "target", "hash", "cfghash", "2026-01-02") != base
