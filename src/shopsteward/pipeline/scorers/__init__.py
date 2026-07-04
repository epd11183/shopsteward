"""Scorer registry: pluggable scoring stages the orchestrator runs in order.

Submodules register themselves at import time (`register(Scorer())` at module
scope), so importing this package populates `REGISTRY` as a side effect. Order
matters: it mirrors the tuning-profile weights order (technical, commercial,
catalog_gap, historical_conversion).
"""

from dataclasses import dataclass, field
from typing import Protocol

from shopsteward.adapters.vision.interface import VisionAdapter
from shopsteward.pipeline.models import TuningProfile

__all__ = [
    "REGISTRY",
    "ScoreContext",
    "Scorer",
    "ScorerResult",
    "get_registered",
    "register",
]


@dataclass
class ScoreContext:
    user_id: int
    photo_id: str
    base_name: str
    jpeg_path: str
    profile: TuningProfile
    vision: VisionAdapter
    rescore: bool = False


@dataclass
class ScorerResult:
    score: float
    detail: dict = field(default_factory=dict)


class Scorer(Protocol):
    name: str

    def score(self, ctx: ScoreContext) -> ScorerResult | None: ...


REGISTRY: dict[str, Scorer] = {}


def register(scorer: Scorer) -> None:
    REGISTRY[scorer.name] = scorer


def get_registered() -> list[Scorer]:
    return list(REGISTRY.values())


# Imported for side effect: each module calls register() at module scope.
from shopsteward.pipeline.scorers import commercial, stubs, technical  # noqa: E402,F401
