"""The Capability Protocol + the in-process registry (M8a spec §3,
draft §2.3/§8.1). This PR ships ZERO real capabilities -- the registry is
exercised entirely against a StubCapability defined in tests/.

Invariant enforced at register() time, not by review (draft §2.3 invariant
1): a capability with no working undo() cannot register above T2 (i.e. its
max_tier must not be T0/AUTO or T1/NOTIFY without an undo()). undo is
"missing" if the attribute is None or not callable -- a capability that
genuinely has no undo path must set `undo = None` explicitly rather than
omitting the method, so the registry can tell "no undo" apart from a typo.

max_tier is a Python attribute on the capability object, not config (draft
§2.3 invariant 2): config can never RAISE the effective tier above this
Python ceiling. There is no per-capability config-lowering knob yet
(tiers.effective_tier() takes no config param) -- add one in a later PR
when the first real capability needs it (YAGNI)."""

import hashlib
from typing import Protocol, runtime_checkable

from shopsteward.pipeline.ops.models import ExecutionResult, OpsConfig, ProposedAction, Tier


@runtime_checkable
class Capability(Protocol):
    key: str
    max_tier: Tier
    policy_verified: bool

    def propose(self, conn: object, user_id: int, cfg: OpsConfig) -> list[ProposedAction]:
        """Reads only. Must not mutate anything outside `conn`."""
        ...

    def execute(self, conn: object, user_id: int, action: ProposedAction) -> ExecutionResult: ...

    def undo(self, conn: object, user_id: int, action: ProposedAction) -> None:
        """Required (a real callable) iff max_tier < Tier.PROPOSE."""
        ...

    def estimate_cost_usd(self, action: ProposedAction) -> float: ...


REGISTRY: dict[str, Capability] = {}


def compute_action_id(
    capability: str, target_id: str, inputs_hash: str, ops_config_hash: str, day: str
) -> str:
    """`sha256(capability|target_id|inputs_hash|ops_config_hash|day)`
    (M8a spec §3, draft §8.3) -- the idempotency key. Shared here so every
    capability (and its tests) computes it identically."""
    raw = "|".join((capability, target_id, inputs_hash, ops_config_hash, day))
    return hashlib.sha256(raw.encode()).hexdigest()


def register(cap: Capability) -> None:
    if cap.max_tier < Tier.PROPOSE and not callable(getattr(cap, "undo", None)):
        raise ValueError(
            f"capability {cap.key!r}: max_tier={cap.max_tier.name} requires a "
            "working undo() to register above T2 (draft §2.3 invariant 1)"
        )
    REGISTRY[cap.key] = cap
