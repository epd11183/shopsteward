"""Buffer adapter models (design docs/designs/2026-11-22-buffer-social-adapter.md §3).
No IDs in config, docs or fixtures (public repo): channels are keyed by service
name and resolved at runtime."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Service = Literal["instagram", "facebook", "pinterest"]


class BufferChannel(BaseModel):
    channel_id: str
    service: Service
    name: str
    is_disconnected: bool = False
    is_locked: bool = False
    is_queue_paused: bool = False
    boards: dict[str, str] = Field(default_factory=dict)  # pinterest board name -> serviceId


class BufferPostSpec(BaseModel):
    channel_id: str
    text: str
    service: Service | None = None
    image_url: str | None = None
    mode: Literal["addToQueue", "customScheduled"] = "customScheduled"
    due_at: datetime | None = None
    scheduling_type: Literal["automatic", "notification"] = "automatic"
    pin_title: str | None = None
    pin_url: str | None = None
    pin_board_service_id: str | None = None
    tag_ids: list[str] = Field(default_factory=list)
    ai_assisted: bool = True


class BufferPost(BaseModel):
    post_id: str
    channel_id: str
    status: str
    text: str = ""
    due_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime | None = None
    external_link: str | None = None
    pin_url: str | None = None
    error_message: str | None = None
    # ABSENT != 0: a metric Buffer did not report is simply missing, never zero-filled.
    metrics: dict[str, float] = Field(default_factory=dict)
    metrics_updated_at: datetime | None = None
