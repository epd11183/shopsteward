"""Look adapter protocol. Mirrors adapters.copy: the model owns its own
develop-settings schema; no image and no white balance are ever produced here
(WB is trusted as-shot per the RAW-auto-edit design)."""

from typing import Protocol

from pydantic import BaseModel, Field


class LookProfile(BaseModel):
    name: str
    description: str = ""
    contrast: int = Field(default=0, ge=-100, le=100)
    tone_curve: list[list[int]] = Field(default_factory=list)  # [[x, y], ...]
    hsl: dict[str, int] = Field(default_factory=dict)
    split_toning: dict[str, int] = Field(default_factory=dict)
    vibrance: int = Field(default=0, ge=-100, le=100)
    saturation: int = Field(default=0, ge=-100, le=100)


class LookUsage(BaseModel):
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    est_cost_usd: float | None = None


class LookResult(BaseModel):
    profile: LookProfile
    usage: LookUsage | None = None  # None => fake/fixture mode, no llm.call event


class LookAdapter(Protocol):
    def generate_look(self, description: str, *, model: str) -> LookResult: ...


class LookParseError(RuntimeError):
    """Raised when a look-provider response cannot be parsed into a LookProfile."""
