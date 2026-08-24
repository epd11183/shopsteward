"""Fixture-backed adapter: the default until live access is approved (no
`live.py` exists at all yet -- design doc §1.3/§1.4). Same split
`adapters/etsy/fake.py` already uses: `FixturePinterestAdapter` for reads
(boards/pins/analytics loaded from scrubbed JSON), `FakePinterestWriteAdapter`
in-memory for writes. No fixture may contain a real board/pin id or the
shop's account id (public repo)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from shopsteward.adapters.pinterest.interface import PinterestWriteError
from shopsteward.adapters.pinterest.models import (
    AccountAnalytics,
    BoardSpec,
    PinAnalytics,
    PinMetric,
    PinSpec,
    PinterestBoard,
    PinterestBoardRef,
    PinterestPin,
    PinterestPinRef,
)


class FixturePinterestAdapter:
    def __init__(self, fixture_dir: Path):
        self._dir = Path(fixture_dir)

    def _load(self, name: str) -> dict[str, Any]:
        return json.loads((self._dir / f"{name}.json").read_text())

    def list_boards(self) -> list[PinterestBoard]:
        return [PinterestBoard.model_validate(r) for r in self._load("boards")["results"]]

    def get_board(self, board_id: str) -> PinterestBoard:
        for board in self.list_boards():
            if board.board_id == board_id:
                return board
        raise KeyError(f"unknown Pinterest board_id {board_id}")

    def list_pins(self, board_id: str) -> list[PinterestPin]:
        rows = self._load("pins")["results"]
        return [PinterestPin.model_validate(r) for r in rows if r.get("board_id") == board_id]

    def get_pin(self, pin_id: str) -> PinterestPin:
        for r in self._load("pins")["results"]:
            if r.get("pin_id") == pin_id:
                return PinterestPin.model_validate(r)
        raise KeyError(f"unknown Pinterest pin_id {pin_id}")

    def pin_analytics(
        self, pin_id: str, *, start: date, end: date, metrics: list[PinMetric]
    ) -> PinAnalytics:
        rows = self._load("pin_analytics").get(pin_id, {})
        found = {PinMetric(m): rows[m] for m in metrics if m in rows}
        return PinAnalytics(pin_id=pin_id, start=start, end=end, metrics=found)

    def account_analytics(
        self, *, start: date, end: date, metrics: list[PinMetric]
    ) -> AccountAnalytics:
        rows = self._load("account_analytics")
        found = {PinMetric(m): rows[m] for m in metrics if m in rows}
        return AccountAnalytics(start=start, end=end, metrics=found)


class FakePinterestWriteAdapter:
    """In-memory PinterestWriteAdapter twin -- the default everywhere
    (tests + the offline default). `create_board` mints `board-{n}`,
    `create_pin` mints `pin-{n}`; `delete_pin` pops and raises
    `PinterestWriteError(404, ...)` on a missing id. `calls` records every
    method invocation (name, kwargs) for assertions -- FakeEtsyWriteAdapter
    precedent."""

    def __init__(self) -> None:
        self._next_board_id = 1
        self._next_pin_id = 1
        self.boards: dict[str, dict[str, Any]] = {}
        self.pins: dict[str, PinterestPin] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_board(self, spec: BoardSpec) -> PinterestBoardRef:
        board_id = f"board-{self._next_board_id}"
        self._next_board_id += 1
        self.boards[board_id] = {"name": spec.name, "description": spec.description}
        self.calls.append(("create_board", {"board_id": board_id, "spec": spec}))
        return PinterestBoardRef(board_id=board_id)

    def create_pin(self, spec: PinSpec) -> PinterestPinRef:
        pin_id = f"pin-{self._next_pin_id}"
        self._next_pin_id += 1
        self.pins[pin_id] = PinterestPin(
            pin_id=pin_id,
            board_id=spec.board_id,
            link=spec.link,
            title=spec.title,
            description=spec.description,
            created_at=datetime.now(UTC),
            media_url=spec.media.url,
        )
        self.calls.append(("create_pin", {"pin_id": pin_id, "spec": spec}))
        return PinterestPinRef(pin_id=pin_id)

    def delete_pin(self, pin_id: str) -> None:
        if pin_id not in self.pins:
            raise PinterestWriteError(404, f"unknown Pinterest pin_id {pin_id}")
        del self.pins[pin_id]
        self.calls.append(("delete_pin", {"pin_id": pin_id}))
