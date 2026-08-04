"""In-memory PrintFileHost twin -- the default everywhere; slices 1-4 run
entirely offline on this (design §4.2). key/url are deterministic (sha256 of
the bytes), so build-stage tests can assert on them without touching a real
host or a network. expires_at is a fixed sentinel, never derived from a
clock (the fake stays pure); `calls` records every invocation (name,
kwargs), mirroring FakeGelatoAdapter/FakeEtsyWriteAdapter."""

import hashlib
from typing import Any

from shopsteward.adapters.printfile.interface import HostedFile


class FakePrintFileHost:
    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def publish(self, data: bytes, *, name: str, ttl_seconds: int) -> HostedFile:
        key = hashlib.sha256(data).hexdigest()
        self._files[key] = data
        self.calls.append(
            ("publish", {"key": key, "name": name, "ttl_seconds": ttl_seconds, "bytes": len(data)})
        )
        return HostedFile(
            key=key, url=f"https://fake.invalid/{key}", expires_at="9999-12-31T00:00:00Z"
        )

    def revoke(self, key: str) -> None:
        self._files.pop(key, None)
        self.calls.append(("revoke", {"key": key}))
