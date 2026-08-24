"""Adapter protocol. Core code depends on this, never on an SDK/HTTP client.
Shape mirrors `adapters/etsy/interface.py`: read Protocol + write Protocol +
a single error class. No `live.py` exists yet (design doc §1.4 -- an OAuth
app/business account/policy verdict are all NOT approved)."""

from datetime import date
from typing import Protocol

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

_MAX_ERROR_LEN = 500


class PinterestAdapter(Protocol):
    def list_boards(self) -> list[PinterestBoard]: ...
    def get_board(self, board_id: str) -> PinterestBoard: ...
    def list_pins(self, board_id: str) -> list[PinterestPin]: ...
    def get_pin(self, pin_id: str) -> PinterestPin: ...
    def pin_analytics(
        self, pin_id: str, *, start: date, end: date, metrics: list[PinMetric]
    ) -> PinAnalytics:
        """Returns a PinAnalytics whose `metrics` omits any metric Pinterest
        did not report for the window. A pin younger than Pinterest's
        analytics lag reports nothing -- that is absence of data, never
        zero."""
        ...

    def account_analytics(
        self, *, start: date, end: date, metrics: list[PinMetric]
    ) -> AccountAnalytics: ...


class PinterestWriteError(RuntimeError):
    """Raised by PinterestWriteAdapter implementations on any write failure.
    EtsyWriteError twin, verbatim shape: carries only the HTTP status and
    Pinterest's `error` field -- never the raw response body; the message
    is truncated defensively."""

    def __init__(self, status_code: int, error: str | None) -> None:
        self.status_code = status_code
        self.error = error
        message = f"Pinterest write failed with HTTP {status_code}"
        if error:
            message += f": {error[:_MAX_ERROR_LEN]}"
        super().__init__(message)


class PinterestWriteAdapter(Protocol):
    def create_board(self, spec: BoardSpec) -> PinterestBoardRef: ...
    def create_pin(self, spec: PinSpec) -> PinterestPinRef: ...
    def delete_pin(self, pin_id: str) -> None:
        """The real undo path for the eventual live `social.pinterest_post`
        -- unlike `social.caption_draft` (nothing to reverse) a published
        pin is genuinely deletable. Also smoke-test cleanup."""
        ...
