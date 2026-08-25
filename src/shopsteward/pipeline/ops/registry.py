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

from shopsteward.adapters.planner.interface import ProposalIntent
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

    def materialize(
        self, conn: object, user_id: int, cfg: OpsConfig, intent: ProposalIntent
    ) -> ProposedAction | None:
        """The LLM-planner grounding hook (M8b slice 2, design §2): re-derive
        a ProposedAction for `intent.target_id` from the SAME deterministic
        candidates propose() would build, or None if that target isn't one of
        them (a hallucinated/ineligible target). Must share one grounding
        function with propose() so the two can never disagree."""
        ...


class StaleTargetError(ValueError):
    """H2b (guardrail review, 2026-08-25 -- the 4th instance of one failure
    class: fake-adapter approve, run/undo, catalog_expand pace, social.
    caption_draft's own channel/cooldown drift). `execute()`'s own
    re-validation is safety-critical AND easy to get subtly wrong: a rate/
    policy condition (cooldown, eligibility mode, weekly pace...) baked into
    the SAME grounding predicate `execute()` re-checks is indistinguishable
    from genuine per-target staleness (the listing/file/target is actually
    gone) and, raised as a plain exception, gets caught by
    `runner._execute_and_record` and terminalized as `action.failed` --
    PERMANENTLY burning that action_id, even though the underlying
    condition (a cooldown, a pace cap) is temporary and should leave the
    proposal approvable again once it passes. Each prior instance was fixed
    ad hoc, one capability at a time; this is the structural stop.

    The runner's default for ANY exception raised by `cap.execute()` is now
    the SAFE one: non-terminal (recorded as `action.refused`, same state a
    normal `governor.govern()` refusal leaves an action in -- still
    "proposed"/re-approvable, never permanently burned). A capability must
    explicitly OPT IN to terminalizing by raising `StaleTargetError`
    (a `ValueError` subclass, so existing `pytest.raises(ValueError)`
    assertions still pass) -- reserved for a genuine per-target check that
    can never un-happen on its own (the listing was deactivated, the file
    was already ingested, the draft was already built): raise it ONLY for
    that, never for a rate/policy condition that governor.govern() should
    have refused before execute() was ever reached."""


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
