"""Fixture-backed and fully-programmable fake vision adapters for tests.

Deviation from the M3 design doc: the design describes `FixtureVisionAdapter`
verdicts as keyed by `base_name` read from `tests/fixtures/vision/*.json`, with
unknown names falling back to a deterministic pseudo-verdict derived from
`sha256(base_name)`. The `VisionAdapter` protocol only receives `jpeg_bytes`
(no `base_name`), so threading a filename through would mean widening the
protocol for a feature no M3 test actually exercises — the orchestrator tests
use `FakeVisionAdapter` for precise, ordered control instead. This
implementation keeps the fixture-directory constructor arg (for forward
compatibility) but always falls back to the deterministic pseudo-verdict,
derived from `sha256(jpeg_bytes)` rather than a base name, so it stays stable
within a single scoring run. The canned-file lookup is YAGNI until a caller
needs it.
"""

import hashlib
from pathlib import Path

from shopsteward.adapters.vision.interface import (
    CopyCheckResult,
    CopyCheckVerdict,
    VisionResult,
    VisionVerdict,
)


class FixtureVisionAdapter:
    def __init__(self, fixture_dir: Path | None = None):
        self._dir = Path(fixture_dir) if fixture_dir is not None else None

    def score_commercial(self, jpeg_bytes: bytes, *, model: str) -> VisionResult:
        digest = hashlib.sha256(jpeg_bytes).hexdigest()
        score = 30 + (int(digest[:8], 16) % 61)
        verdict = VisionVerdict(
            commercial_score=score,
            subject=f"fixture subject ({digest[:8]})",
            strongest_room_style="neutral",
            one_risk="none flagged (fixture)",
            rationale=f"deterministic fixture verdict, score={score}",
            scene="fixture scene: one animal standing on rocks under a clear sky.",
        )
        return VisionResult(verdict=verdict, usage=None)


class FixtureCopyChecker:
    """Offline default for the pre-publish copy check. It cannot see anything,
    so it NEVER approves: a fixture run must not auto-publish a real listing."""

    def check_copy(
        self, jpeg_bytes: bytes, *, title: str, tags: list[str], description: str, model: str
    ) -> CopyCheckResult:
        return CopyCheckResult(
            verdict=CopyCheckVerdict(
                ok=False, problems=["copy not verified against the photo (fixture checker)"]
            )
        )


class FakeCopyChecker:
    """Programmable checker for tests: queued CopyCheckResult | Exception."""

    def __init__(self, results: list[CopyCheckResult | Exception]):
        self._results = list(results)
        self.calls: list[dict] = []

    def check_copy(
        self, jpeg_bytes: bytes, *, title: str, tags: list[str], description: str, model: str
    ) -> CopyCheckResult:
        self.calls.append({"title": title, "tags": tags, "description": description})
        if not self._results:
            raise RuntimeError("FakeCopyChecker exhausted: no more queued results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeVisionAdapter:
    def __init__(self, results: list[VisionResult | Exception]):
        self._results = list(results)
        self.calls: list[tuple[int, str]] = []

    def score_commercial(self, jpeg_bytes: bytes, *, model: str) -> VisionResult:
        self.calls.append((len(jpeg_bytes), model))
        if not self._results:
            raise RuntimeError("FakeVisionAdapter exhausted: no more queued results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
