"""In-memory Meta adapter for tests and offline mode.

FakeMeta implements both IgPublisher and PagePublisher protocols, tracking
all posts in memory. This is the default adapter everywhere (tests + offline
mode) until live posting is approved (PRD §8.4).

The fake enforces the same invariants as the real adapter:
- schedule_post() generates and returns a unique media/post ID
- unschedule_post() removes the post (subsequent accesses raise MetaGraphError)
- Posts are indexed by platform + ID
- Token validation is not performed (real adapter would check scopes)

`posts` dict lets tests assert on post lifecycle; `calls` list records every
method invocation for call-sequence verification.
"""

from datetime import datetime
from typing import Any

from shopsteward.adapters.meta.interface import (
    MetaGraphError,
    MetaPost,
    Platform,
)


class FakeMeta:
    """In-memory implementation of both IgPublisher and PagePublisher.

    Usage:
        fake = FakeMeta()
        meta_publisher: IgPublisher = fake
        media_id = fake.schedule_post(post, page_access_token="token")
        assert fake.posts[Platform.INSTAGRAM][media_id] is not None
        fake.unschedule_post(media_id, page_access_token="token")
        assert media_id not in fake.posts[Platform.INSTAGRAM]
    """

    def __init__(self) -> None:
        self._next_id = 1000
        # posts[platform] -> {media_id/post_id: MetaPost}
        self.posts: dict[Platform, dict[str, MetaPost]] = {
            Platform.INSTAGRAM: {},
            Platform.FACEBOOK: {},
        }
        # Record all method calls for assertions: (method_name, kwargs)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def schedule_post(self, post: MetaPost, *, page_access_token: str) -> str:
        """Schedule or publish a post. Returns a unique media/post ID.

        Raises MetaGraphError if page_access_token is empty or post is invalid.
        """
        if not page_access_token:
            raise MetaGraphError(
                "empty page_access_token",
                status_code=400,
                error_code="INVALID_TOKEN",
            )

        media_id = f"meta_{self._next_id}"
        self._next_id += 1

        # Store the post with the generated ID
        self.posts[post.platform][media_id] = post.model_copy(
            update={
                "meta_media_id": media_id,
                "published_at": datetime.now() if post.scheduled_for is None else post.published_at,
            }
        )

        self.calls.append(
            (
                "schedule_post",
                {
                    "platform": post.platform,
                    "media_id": media_id,
                    "caption_len": len(post.caption),
                    "hashtag_count": len(post.hashtags),
                },
            )
        )

        return media_id

    def unschedule_post(self, media_id: str, *, page_access_token: str) -> None:
        """Delete a post by its media/post ID.

        Raises MetaGraphError if the post is not found.
        """
        if not page_access_token:
            raise MetaGraphError(
                "empty page_access_token",
                status_code=400,
                error_code="INVALID_TOKEN",
            )

        # Try to find and remove the post from either platform
        for platform in [Platform.INSTAGRAM, Platform.FACEBOOK]:
            if media_id in self.posts[platform]:
                del self.posts[platform][media_id]
                self.calls.append(
                    (
                        "unschedule_post",
                        {"platform": platform, "media_id": media_id},
                    )
                )
                return

        # Not found in either platform
        raise MetaGraphError(
            f"post {media_id} not found",
            status_code=404,
            error_code="NOT_FOUND",
        )

    def get_post(self, media_id: str) -> MetaPost | None:
        """Retrieve a post by ID from either platform. Returns None if not found."""
        for platform in [Platform.INSTAGRAM, Platform.FACEBOOK]:
            if media_id in self.posts[platform]:
                return self.posts[platform][media_id]
        return None

    def list_posts(self, platform: Platform) -> list[MetaPost]:
        """List all posts for a given platform."""
        return list(self.posts[platform].values())

    def clear(self) -> None:
        """Reset all state (for test isolation)."""
        self._next_id = 1000
        self.posts = {Platform.INSTAGRAM: {}, Platform.FACEBOOK: {}}
        self.calls = []
