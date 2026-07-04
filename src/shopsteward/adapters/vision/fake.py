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

from shopsteward.adapters.vision.interface import VisionResult, VisionVerdict


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
        )
        return VisionResult(verdict=verdict, usage=None)


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
