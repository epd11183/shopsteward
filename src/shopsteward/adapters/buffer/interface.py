"""Adapter protocols. Core code depends on this, never on httpx or an SDK."""

from datetime import datetime
from typing import Protocol

from shopsteward.adapters.buffer.models import BufferChannel, BufferPost, BufferPostSpec

_MAX_ERROR_LEN = 500


class BufferError(RuntimeError):
    """Any Buffer failure. `code` is one of UNAUTHORIZED, FORBIDDEN, NOT_FOUND,
    RATE_LIMITED, MUTATION, TRANSPORT. The message is truncated defensively and
    never carries the raw response body."""

    def __init__(self, code: str, message: str = "", *, retry_after_s: int | None = None) -> None:
        self.code = code
        self.retry_after_s = retry_after_s
        text = f"Buffer {code}"
        if message:
            text += f": {message[:_MAX_ERROR_LEN]}"
        super().__init__(text)


class BufferUnknownOutcome(BufferError):
    """Timeout/5xx after a mutation was sent: the post MAY exist -> reconcile."""


class BufferAdapter(Protocol):
    def list_channels(self) -> list[BufferChannel]: ...

    def list_posts(
        self,
        *,
        channel_ids: list[str],
        created_since: datetime,
        statuses: list[str] | None = None,
        with_metrics: bool = False,
    ) -> list[BufferPost]: ...


class BufferWriteAdapter(Protocol):
    def create_post(self, spec: BufferPostSpec) -> BufferPost: ...

    def delete_post(self, post_id: str) -> None: ...
