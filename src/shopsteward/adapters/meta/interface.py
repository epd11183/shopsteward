"""Meta Graph API adapter protocol for Instagram + Facebook posting.

Core code depends on these protocols, never on the Meta SDK or httpx directly.
Per CLAUDE.md and M6 design §4 & §7, this module is the sole public API;
all HTTP calls are encapsulated in adapters (LiveMeta) or mocks (FakeMeta).
"""

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class Platform(StrEnum):
    """Target platform for a scheduled post."""

    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class PostStatus(StrEnum):
    """Lifecycle status of a post in the queue."""

    PROPOSED = "proposed"  # Pending operator approval
    APPROVED = "approved"  # Ready to schedule
    SCHEDULED = "scheduled"  # Scheduled for future publish
    PUBLISHED = "published"  # Successfully published to Meta
    FAILED = "failed"  # Permanent failure (not retryable)
    UNDONE = "undone"  # Undo was requested and executed


class MetaPost(BaseModel):
    """A post queued for scheduling to Instagram or Facebook.

    `image_ref` is a file path or asset ID (platform-dependent, determined
    at scheduling time). `caption` may be empty at proposal time (lazily
    generated at schedule time if `caption_generation.mode` is "gate3_or_manual"
    per config). `hashtags` are independent of caption: caption is prose,
    hashtags are the tag strategy from config/defaults/meta.json.
    """

    id: str = Field(description="Unique post identifier (UUID or auto-generated)")
    platform: Platform
    image_ref: str = Field(description="File path or asset ID for the image")
    caption: str = Field(default="", description="Post caption text")
    hashtags: list[str] = Field(default_factory=list, description="List of hashtags to include")
    scheduled_for: datetime | None = Field(
        default=None, description="When to publish (None = not yet scheduled)"
    )
    status: PostStatus = Field(default=PostStatus.PROPOSED)
    created_at: datetime = Field(description="When the post was created in the queue")
    published_at: datetime | None = Field(default=None, description="When it was published")
    meta_media_id: str | None = Field(
        default=None, description="Opaque ID from Meta (set after scheduling)"
    )


class IgPublisher(Protocol):
    """Publish and manage Instagram posts via Meta Graph API."""

    def schedule_post(self, post: MetaPost, *, page_access_token: str) -> str:
        """Schedule or publish an Instagram post. Returns the Meta media ID.

        Raises MetaGraphError on API failure.
        """
        ...

    def unschedule_post(self, media_id: str, *, page_access_token: str) -> None:
        """Delete a scheduled (or published) Instagram post by its Meta media ID.

        Raises MetaGraphError if the post is not found or if deletion fails.
        """
        ...


class PagePublisher(Protocol):
    """Publish and manage Facebook Page posts via Meta Graph API."""

    def schedule_post(self, post: MetaPost, *, page_access_token: str) -> str:
        """Schedule or publish a Facebook Page post. Returns the Meta post ID.

        Raises MetaGraphError on API failure.
        """
        ...

    def unschedule_post(self, post_id: str, *, page_access_token: str) -> None:
        """Delete a scheduled (or published) Facebook post by its Meta post ID.

        Raises MetaGraphError if the post is not found or if deletion fails.
        """
        ...


class MetaGraphError(RuntimeError):
    """Raised by adapter implementations on any Meta Graph API error.

    Carries HTTP status, error code, and message (all may be None if the
    error is a network failure or parsing error).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)
