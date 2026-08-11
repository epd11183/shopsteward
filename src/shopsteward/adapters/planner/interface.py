"""Planner adapter protocol for the LLM-narrated Brief (M8b slice 1, design
§5). PlannerNarration/PlannerUsage are owned here (not in `pipeline.ops`) for
the same reason CopyInputs/CopyVerdict are owned by `adapters.copy.interface`:
import-linter contracts make this a one-way relationship, `pipeline` may
import `adapters.planner`, but no adapter may import `pipeline`. That is also
why `narrate()` takes a plain rendered brief `str` rather than a
`pipeline.ops.models.Brief` -- the adapter never sees the pipeline's own
models, only the already-rendered text."""

from typing import Protocol

from pydantic import BaseModel


class PlannerUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    est_cost_usd: float


class PlannerNarration(BaseModel):
    text: str
    usage: PlannerUsage


class PlannerAdapter(Protocol):
    def narrate(self, deterministic_brief_text: str) -> PlannerNarration: ...


class PlannerParseError(RuntimeError):
    """Raised when a planner-provider response cannot be parsed into a
    PlannerNarration (transport/parse failure -- mirrors CopyParseError)."""
