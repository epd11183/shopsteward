"""In-memory Buffer adapter (read + write). Mints `post-{n}` ids."""

from datetime import UTC, datetime

from shopsteward.adapters.buffer.interface import BufferError, BufferUnknownOutcome
from shopsteward.adapters.buffer.models import BufferChannel, BufferPost, BufferPostSpec


class FakeBufferAdapter:
    def __init__(self, channels: list[BufferChannel] | None = None) -> None:
        self.channels = list(channels or [])
        self.posts: dict[str, BufferPost] = {}
        self.create_count = 0
        # Simulates a timeout AFTER a successful create (the post exists).
        self.fail_after_create_once = False

    def list_channels(self) -> list[BufferChannel]:
        return list(self.channels)

    def list_posts(
        self,
        *,
        channel_ids: list[str],
        created_since: datetime,
        statuses: list[str] | None = None,
        with_metrics: bool = False,
    ) -> list[BufferPost]:
        out = []
        for p in self.posts.values():
            if p.channel_id not in channel_ids:
                continue
            if p.created_at is not None and p.created_at < created_since:
                continue
            if statuses is not None and p.status not in statuses:
                continue
            out.append(p if with_metrics else p.model_copy(update={"metrics": {}}))
        return out

    def create_post(self, spec: BufferPostSpec) -> BufferPost:
        self.create_count += 1
        post = BufferPost(
            post_id=f"post-{self.create_count}",
            channel_id=spec.channel_id,
            status="scheduled",
            text=spec.text,
            due_at=spec.due_at,
            created_at=datetime.now(UTC),
            pin_url=spec.pin_url,
        )
        self.posts[post.post_id] = post
        if self.fail_after_create_once:
            self.fail_after_create_once = False
            raise BufferUnknownOutcome("TRANSPORT", "timeout after send")
        return post

    def delete_post(self, post_id: str) -> None:
        if self.posts.pop(post_id, None) is None:
            raise BufferError("NOT_FOUND", f"unknown post {post_id}")
