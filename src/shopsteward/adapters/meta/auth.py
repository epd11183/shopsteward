"""Meta OAuth2 token acquisition and storage.

This module handles token storage only -- no auth flow (that's slice 2).
Tokens are stored locally and never logged, printed, or committed.

Following the EtsyTokenStore pattern exactly: atomic file writes (tmp file +
os.replace), schema versioning, reload-on-use.
"""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from shopsteward.settings import meta_tokens_path

_SCHEMA = "shopsteward.metatokens/1"


class MetaTokens(BaseModel):
    """A Page Access Token record with lifecycle metadata.

    Per M6 design §7 (M6 research question M6): long-lived tokens are
    60 days from issue. Refresh via GET /refresh_access_token once the
    token is >=24h old. The runner must proactively check age and refresh
    before each scheduled run.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field(default=_SCHEMA, alias="schema")
    access_token: str = Field(description="Page Access Token from Meta")
    issued_at: float = Field(description="Unix timestamp when token was issued")
    refresh_at: float = Field(
        description="Unix timestamp when refresh is allowed (>=24h after issue)"
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes granted (e.g., instagram_basic_publish, pages_manage_posts)",
    )
    page_id: str | None = Field(default=None, description="Meta page ID (discovered at auth time)")
    instagram_account_id: str | None = Field(
        default=None, description="Linked Instagram account ID (if connected)"
    )


class MetaAuthStore:
    """Reads/writes the local Meta token file.

    File writes are atomic (tmp file + os.replace), mirroring EtsyTokenStore.
    Readers never see a partial file. Tokens are never logged.
    """

    def __init__(self, path: Path | None = None):
        self._path = path if path is not None else meta_tokens_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> MetaTokens | None:
        """Load tokens from disk. Returns None if file does not exist."""
        if not self._path.exists():
            return None
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        schema = raw.get("schema")
        if schema != _SCHEMA:
            raise ValueError(f"unexpected Meta token file schema: {schema!r}")
        return MetaTokens.model_validate(raw)

    def save(self, tokens: MetaTokens) -> None:
        """Atomically write tokens to disk. Parent directory is created if needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        part_path = self._path.with_name(self._path.name + ".part")
        payload = tokens.model_dump(by_alias=True)
        with part_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part_path, self._path)

    def needs_refresh(
        self,
        *,
        now: Callable[[], float] = time.time,
    ) -> bool:
        """Check whether tokens need refreshing before the next scheduled run.

        Per M6 design §7 (M6), refresh is allowed once token is >=24h old.
        Returns True if the token is at or past the refresh threshold.
        """
        tokens = self.load()
        if tokens is None:
            return False
        return now() >= tokens.refresh_at

    def token_expires_at(
        self,
        *,
        now: Callable[[], float] = time.time,
    ) -> float | None:
        """Return Unix timestamp when token expires (60 days from issue).

        Returns None if no token is stored.
        """
        tokens = self.load()
        if tokens is None:
            return None
        return tokens.issued_at + (60 * 24 * 3600)  # 60 days
