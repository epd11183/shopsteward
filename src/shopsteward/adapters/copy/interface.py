"""Copy adapter protocol for Etsy listing copy generation (PRD §13 decision 38).

CopyInputs/CopyVerdict are owned here (not in `pipeline.listings.models`) for
the same reason `VisionVerdict` is owned by `adapters.vision.interface`:
import-linter contracts make this a one-way relationship, `pipeline` may
import `adapters.copy`, but no adapter may import `pipeline`.
`pipeline.listings.models` re-exports `CopyInputs` for backward-compatible
imports (VisionVerdict precedent).
"""

from typing import Protocol

from pydantic import BaseModel, Field, field_validator

_MAX_TITLE_LEN = 140
_MAX_TAGS = 13
_MAX_TAG_LEN = 20


class CopyInputs(BaseModel):
    """Everything the copy prompt template needs: house style guide text +
    per-photo vision-verdict signals (from proj_scores, when available) +
    digital-format facts (from mockups.json whatyougot). Never the photograph
    itself -- AI never touches the photograph (CLAUDE.md)."""

    house_style: str
    subject: str | None = None
    strongest_room_style: str | None = None
    one_risk: str | None = None
    rationale: str | None = None
    orientation: str
    format: str = "digital_download"
    sizes: list[str]
    formats: list[str]


class CopyVerdict(BaseModel):
    title: str = Field(min_length=1, max_length=_MAX_TITLE_LEN)
    tags: list[str] = Field(min_length=1, max_length=_MAX_TAGS)
    description: str = Field(min_length=1)
    materials: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def _tags_within_length(cls, tags: list[str]) -> list[str]:
        for tag in tags:
            if not tag.strip():
                raise ValueError("empty tag not allowed (Etsy rejects blank tags)")
            if len(tag) > _MAX_TAG_LEN:
                raise ValueError(f"tag {tag!r} exceeds {_MAX_TAG_LEN} chars")
        return tags


class CopyUsage(BaseModel):
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    est_cost_usd: float | None = None


class CopyResult(BaseModel):
    verdict: CopyVerdict
    usage: CopyUsage | None = None  # None => fixture/fake mode, no llm.call event


class CopyAdapter(Protocol):
    def generate_copy(self, inputs: CopyInputs, *, model: str) -> CopyResult: ...


class CopyParseError(RuntimeError):
    """Raised when a copy-provider response cannot be parsed into a CopyVerdict."""
