"""Planner adapter protocol for the LLM-narrated Brief (M8b slice 1, design
§5) and the intent-proposing planner (M8b slice 2, design §2). PlannerNarration/
PlannerUsage/ProposalIntent/CapabilityDescriptor/PlannerPlan are owned here
(not in `pipeline.ops`) for the same reason CopyInputs/CopyVerdict are owned
by `adapters.copy.interface`: import-linter contracts make this a one-way
relationship, `pipeline` may import `adapters.planner`, but no adapter may
import `pipeline`. That is also why `narrate()`/`plan()` take plain rendered
`str`/catalog args rather than `pipeline.ops.models` types -- the adapter
never sees the pipeline's own models, only already-rendered text/JSON.

ProposalIntent is deliberately thin -- it is NEVER an executable call and
NEVER carries an `action_id`/`inputs_hash` (design §2): the pipeline-side
validation gate (`pipeline.ops.planner.plan_proposals`) re-derives every
load-bearing number from real SQL via a capability's `materialize()` before
an intent can become a `ProposedAction`."""

from typing import Protocol

from pydantic import BaseModel, Field


class PlannerUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float


class PlannerNarration(BaseModel):
    text: str
    usage: PlannerUsage


class ProposalIntent(BaseModel):
    """One LLM-proposed intent -- MUST match a registered capability key and
    a target the deterministic grounding already blessed (design §2). `reason`
    is carried through only as commentary; the pipeline never trusts it as an
    audit reason (materialize() sets that from real data)."""

    capability_key: str
    target_id: str
    params: dict[str, str | int | float | bool] = {}
    reason: str = Field(min_length=1)


class CapabilityDescriptor(BaseModel):
    """A static catalog entry the planner is told it may choose from --
    adapter-owned, no pipeline import (design §1)."""

    key: str
    purpose: str
    params_schema: dict[str, str] = {}
    max_tier: int


class PlannerPlan(BaseModel):
    intents: list[ProposalIntent]
    usage: PlannerUsage


class PlannerAdapter(Protocol):
    def narrate(self, deterministic_brief_text: str) -> PlannerNarration: ...

    def plan(self, facts_json: str, catalog: list[CapabilityDescriptor]) -> PlannerPlan: ...


class PlannerParseError(RuntimeError):
    """Raised when a planner-provider response cannot be parsed into a
    PlannerNarration/PlannerPlan (transport/parse failure -- mirrors
    CopyParseError)."""
