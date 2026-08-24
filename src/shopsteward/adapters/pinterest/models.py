"""Pydantic models mirroring the Pinterest v5 API shapes we'll consume once
`live.py` exists (design `docs/designs/2026-08-24-pinterest-adapter-and-loop-roadmap.md`
§1.1). No live adapter exists yet -- these are the contract `fake.py` and
the eventual live implementation both code against."""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class BoardPrivacy(StrEnum):
    PUBLIC = "PUBLIC"
    PROTECTED = "PROTECTED"
    SECRET = "SECRET"


class PinterestBoard(BaseModel):
    board_id: str
    name: str
    description: str = ""
    privacy: BoardPrivacy = BoardPrivacy.PUBLIC
    pin_count: int = 0
    follower_count: int = 0


class BoardSpec(BaseModel):
    name: str
    description: str = ""
    privacy: BoardPrivacy = BoardPrivacy.PUBLIC


class PinterestBoardRef(BaseModel):
    board_id: str


class PinMedia(BaseModel):
    """Pinterest v5 `media_source` -- both real v5 variants. `url` is used
    for `image_url` (Etsy CDN `url_570xN`, public and cheap); `data_b64`/
    `content_type` are used for `image_base64` (a locally-composited
    mockup, not yet public)."""

    source_type: str = Field(pattern="^(image_url|image_base64)$")
    url: str | None = None
    data_b64: str | None = None
    content_type: str | None = None


class PinSpec(BaseModel):
    board_id: str
    media: PinMedia
    link: str
    title: str = Field(max_length=100)
    description: str = Field(max_length=800)
    alt_text: str = Field(default="", max_length=500)
    note: str = ""


class PinterestPin(BaseModel):
    pin_id: str
    board_id: str
    link: str | None = None
    title: str
    description: str
    created_at: datetime
    media_url: str | None = None


class PinterestPinRef(BaseModel):
    pin_id: str


class PinMetric(StrEnum):
    IMPRESSION = "IMPRESSION"
    PIN_CLICK = "PIN_CLICK"
    OUTBOUND_CLICK = "OUTBOUND_CLICK"
    SAVE = "SAVE"


class PinAnalytics(BaseModel):
    """`metrics` omits any metric Pinterest didn't report for the window --
    absent, never zero (mirrors `analytics._views_delta`'s None-means-
    unmeasurable rule, module docstring of the design doc)."""

    pin_id: str
    start: date
    end: date
    metrics: dict[PinMetric, int] = Field(default_factory=dict)


class AccountAnalytics(BaseModel):
    start: date
    end: date
    metrics: dict[PinMetric, int] = Field(default_factory=dict)
