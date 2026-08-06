"""Meta Graph API adapter for Instagram + Facebook posting.

Public API: protocols + models + fake adapter. Core code (pipeline/meta/)
imports only from this module, never from _live.py or other implementation
details (CLAUDE.md).
"""

from shopsteward.adapters.meta.auth import MetaAuthStore, MetaTokens
from shopsteward.adapters.meta.fake import FakeMeta
from shopsteward.adapters.meta.interface import (
    IgPublisher,
    MetaGraphError,
    MetaPost,
    PagePublisher,
    Platform,
    PostStatus,
)

__all__ = [
    "MetaPost",
    "Platform",
    "PostStatus",
    "IgPublisher",
    "PagePublisher",
    "MetaGraphError",
    "FakeMeta",
    "MetaTokens",
    "MetaAuthStore",
]
