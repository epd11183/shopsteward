"""Vision adapter protocol for commercial viability scoring.

VisionVerdict is owned here (not in `pipeline.models`) because the
import-linter contracts make this a one-way relationship: `pipeline` may
import `adapters.vision`, but no adapter may import `pipeline`. Pydantic
models used only within the pipeline stay in `pipeline.models`, which
re-exports `VisionVerdict` from here for backward-compatible imports.
"""

from typing import Protocol

from pydantic import BaseModel, Field


class VisionVerdict(BaseModel):
    commercial_score: int = Field(ge=0, le=100)
    subject: str
    strongest_room_style: str
    one_risk: str
    rationale: str = Field(max_length=140)


class VisionUsage(BaseModel):
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    est_cost_usd: float | None = None


class VisionResult(BaseModel):
    verdict: VisionVerdict
    usage: VisionUsage | None = None  # None => fixture/fake mode, no llm.call event


class VisionAdapter(Protocol):
    def score_commercial(self, jpeg_bytes: bytes, *, model: str) -> VisionResult: ...
