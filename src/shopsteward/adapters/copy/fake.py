"""Fixture-backed and fully-programmable fake copy adapters for tests
(vision/fake.py precedent)."""

import hashlib

from shopsteward.adapters.copy.interface import CopyInputs, CopyResult, CopyVerdict


class FixtureCopyAdapter:
    """Deterministic pseudo-copy derived from the inputs, valid against
    CopyVerdict (title <=140 chars, 13 tags <=20 chars each). usage=None ->
    no llm.call event (offline default)."""

    def generate_copy(self, inputs: CopyInputs, *, model: str) -> CopyResult:
        digest = hashlib.sha256(
            f"{inputs.subject}|{inputs.strongest_room_style}|{inputs.orientation}".encode()
        ).hexdigest()[:8]
        subject = (inputs.subject or "wildlife").title()
        style = (inputs.strongest_room_style or "cabin").title()

        verdict = CopyVerdict(
            title=f"{subject} Wall Art, {style} Decor (Digital Download) [{digest}]",
            tags=[f"tag{i}-{digest}" for i in range(13)],
            description=(
                f"A {subject.lower()} photograph fitted for {style.lower()} interiors. "
                f"Fixture copy, ref {digest}."
            ),
            materials=None,
        )
        return CopyResult(verdict=verdict, usage=None)


class FakeCopyAdapter:
    """Programmable queue (results + usage + exceptions) for ledger + parse-
    failure + soft-cap tests."""

    def __init__(self, results: list[CopyResult | Exception]):
        self._results = list(results)
        self.calls: list[tuple[CopyInputs, str]] = []

    def generate_copy(self, inputs: CopyInputs, *, model: str) -> CopyResult:
        self.calls.append((inputs, model))
        if not self._results:
            raise RuntimeError("FakeCopyAdapter exhausted: no more queued results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
