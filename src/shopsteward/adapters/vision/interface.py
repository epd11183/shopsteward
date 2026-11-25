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
    rationale: str = Field(max_length=500)
    scene: str | None = None


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


class CopyCheckVerdict(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)
    # "" = no sensor dust; otherwise where the dust blobs are ("flat sky, upper left").
    dust: str = ""


class CopyCheckResult(BaseModel):
    verdict: CopyCheckVerdict
    usage: VisionUsage | None = None  # None => fixture/fake mode, no llm.call event


class CopyChecker(Protocol):
    def check_copy(
        self, jpeg_bytes: bytes, *, title: str, tags: list[str], description: str, model: str
    ) -> CopyCheckResult:
        """Does the listing copy claim anything the photo does not show?"""
        ...


class VisionParseError(RuntimeError):
    """Raised when a vision-provider response cannot be parsed into a VisionVerdict."""
