"""Fixture-backed and programmable fake look adapters for tests
(adapters.copy.fake precedent)."""

import hashlib

from shopsteward.adapters.look.interface import LookProfile, LookResult


class FixtureLookAdapter:
    """Deterministic pseudo-look derived from the description. usage=None => no
    llm.call event (offline default)."""

    def generate_look(self, description: str, *, model: str) -> LookResult:
        digest = hashlib.sha256(description.encode()).hexdigest()
        contrast = int(digest[:2], 16) % 40 - 10  # -10..29, stable per description
        vibrance = int(digest[2:4], 16) % 30
        profile = LookProfile(
            name=description,
            description=f"fixture look for {description!r}",
            contrast=contrast,
            tone_curve=[[0, 0], [128, 128], [255, 255]],
            vibrance=vibrance,
        )
        return LookResult(profile=profile, usage=None)


class FakeLookAdapter:
    """Programmable queue (results + exceptions) for ledger/parse-failure tests."""

    def __init__(self, results: list[LookResult | Exception]):
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []

    def generate_look(self, description: str, *, model: str) -> LookResult:
        self.calls.append((description, model))
        if not self._results:
            raise RuntimeError("FakeLookAdapter exhausted: no more queued results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
