"""Adapter protocol for a print-file host (design §4.2). Gelato fetches the
print file from a URL it is given; it does not accept uploaded bytes, so a
local-first tool needs somewhere to host that file transiently. The real
host is a new external service and an unresolved operator decision (design
§17 Q1) -- it gates only the live smoke test, not implementation."""

from typing import Protocol

from pydantic import BaseModel


class HostedFile(BaseModel):
    key: str
    url: str  # transient; returned to the caller, never evented
    expires_at: str


class PrintFileHost(Protocol):
    def publish(self, data: bytes, *, name: str, ttl_seconds: int) -> HostedFile: ...
    def revoke(self, key: str) -> None: ...
