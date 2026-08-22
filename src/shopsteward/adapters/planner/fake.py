"""Fully-programmable fake planner adapter for tests (adapters/copy/fake.py's
FakeCopyAdapter precedent). No network, ever."""

from shopsteward.adapters.planner.interface import (
    CapabilityDescriptor,
    PlannerLimits,
    PlannerNarration,
    PlannerPlan,
    PlannerUsage,
    ProposalIntent,
)


class FakePlannerAdapter:
    """Default/test double. With no queued results, narrate() returns a
    deterministic canned narration (zero cost, records the call). A queue of
    results/exceptions can be supplied to drive specific test scenarios,
    mirroring FakeCopyAdapter.

    `plan` is either a list[ProposalIntent] (returned, zero-cost usage) or an
    Exception (raised) -- omit it and plan() returns zero intents."""

    def __init__(
        self,
        results: list[PlannerNarration | Exception] | None = None,
        plan: list[ProposalIntent] | Exception | None = None,
    ):
        self._results = list(results) if results is not None else None
        self._plan = plan
        self.calls: list[str] = []
        self.plan_calls: list[str] = []

    def narrate(self, deterministic_brief_text: str) -> PlannerNarration:
        self.calls.append(deterministic_brief_text)
        if self._results is None:
            return PlannerNarration(
                text="[narration] the deterministic brief above is unchanged.",
                usage=PlannerUsage(prompt_tokens=0, completion_tokens=0, est_cost_usd=0.0),
            )
        if not self._results:
            raise RuntimeError("FakePlannerAdapter exhausted: no more queued results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def plan(
        self, facts_json: str, catalog: list[CapabilityDescriptor], limits: PlannerLimits
    ) -> PlannerPlan:
        self.plan_calls.append(facts_json)
        if isinstance(self._plan, Exception):
            raise self._plan
        intents = list(self._plan) if self._plan is not None else []
        return PlannerPlan(
            intents=intents,
            usage=PlannerUsage(prompt_tokens=0, completion_tokens=0, est_cost_usd=0.0),
        )
