"""Tests for Meta adapter interface, models, and fake implementation."""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from shopsteward.adapters.meta import (
    FakeMeta,
    MetaAuthStore,
    MetaGraphError,
    MetaPost,
    MetaTokens,
    Platform,
    PostStatus,
)

# --- MetaPost models ---


def test_metapost_creation() -> None:
    now = datetime.now()
    post = MetaPost(
        id="post_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/image.jpg",
        caption="Beautiful sunset!",
        hashtags=["#sunset", "#ocean"],
        scheduled_for=now + timedelta(hours=2),
        status=PostStatus.PROPOSED,
        created_at=now,
    )
    assert post.id == "post_1"
    assert post.platform == Platform.INSTAGRAM
    assert post.caption == "Beautiful sunset!"
    assert post.meta_media_id is None
    assert post.published_at is None


def test_metapost_defaults() -> None:
    now = datetime.now()
    post = MetaPost(
        id="post_1",
        platform=Platform.FACEBOOK,
        image_ref="/path/to/image.jpg",
        created_at=now,
    )
    assert post.caption == ""
    assert post.hashtags == []
    assert post.scheduled_for is None
    assert post.status == PostStatus.PROPOSED
    assert post.meta_media_id is None


# --- FakeMeta ---


def test_fake_schedule_post_instagram() -> None:
    fake = FakeMeta()
    now = datetime.now()
    post = MetaPost(
        id="post_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/image.jpg",
        caption="Test caption",
        hashtags=["#test"],
        created_at=now,
    )

    media_id = fake.schedule_post(post, page_access_token="token123")
    assert media_id.startswith("meta_")
    assert media_id in fake.posts[Platform.INSTAGRAM]
    assert fake.posts[Platform.INSTAGRAM][media_id].caption == "Test caption"


def test_fake_schedule_post_facebook() -> None:
    fake = FakeMeta()
    now = datetime.now()
    post = MetaPost(
        id="post_1",
        platform=Platform.FACEBOOK,
        image_ref="/path/to/image.jpg",
        caption="FB post",
        created_at=now,
    )

    post_id = fake.schedule_post(post, page_access_token="token123")
    assert post_id in fake.posts[Platform.FACEBOOK]
    assert fake.posts[Platform.FACEBOOK][post_id].platform == Platform.FACEBOOK


def test_fake_schedule_post_empty_token_raises() -> None:
    fake = FakeMeta()
    post = MetaPost(
        id="post_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/image.jpg",
        created_at=datetime.now(),
    )

    with pytest.raises(MetaGraphError) as exc_info:
        fake.schedule_post(post, page_access_token="")
    assert exc_info.value.status_code == 400
    assert exc_info.value.error_code == "INVALID_TOKEN"


def test_fake_unschedule_post_instagram() -> None:
    fake = FakeMeta()
    post = MetaPost(
        id="post_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/image.jpg",
        created_at=datetime.now(),
    )

    media_id = fake.schedule_post(post, page_access_token="token123")
    assert media_id in fake.posts[Platform.INSTAGRAM]

    fake.unschedule_post(media_id, page_access_token="token123")
    assert media_id not in fake.posts[Platform.INSTAGRAM]


def test_fake_unschedule_post_facebook() -> None:
    fake = FakeMeta()
    post = MetaPost(
        id="post_1",
        platform=Platform.FACEBOOK,
        image_ref="/path/to/image.jpg",
        created_at=datetime.now(),
    )

    post_id = fake.schedule_post(post, page_access_token="token123")
    fake.unschedule_post(post_id, page_access_token="token123")
    assert post_id not in fake.posts[Platform.FACEBOOK]


def test_fake_unschedule_post_not_found() -> None:
    fake = FakeMeta()
    with pytest.raises(MetaGraphError) as exc_info:
        fake.unschedule_post("meta_9999", page_access_token="token123")
    assert exc_info.value.status_code == 404
    assert exc_info.value.error_code == "NOT_FOUND"


def test_fake_unschedule_post_empty_token_raises() -> None:
    fake = FakeMeta()
    with pytest.raises(MetaGraphError) as exc_info:
        fake.unschedule_post("meta_1000", page_access_token="")
    assert exc_info.value.status_code == 400


def test_fake_get_post() -> None:
    fake = FakeMeta()
    post = MetaPost(
        id="post_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/image.jpg",
        caption="Test",
        created_at=datetime.now(),
    )

    media_id = fake.schedule_post(post, page_access_token="token123")
    retrieved = fake.get_post(media_id)
    assert retrieved is not None
    assert retrieved.caption == "Test"


def test_fake_get_post_not_found() -> None:
    fake = FakeMeta()
    assert fake.get_post("meta_9999") is None


def test_fake_list_posts() -> None:
    fake = FakeMeta()
    now = datetime.now()

    # Add Instagram posts
    ig_post = MetaPost(
        id="ig_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/ig.jpg",
        created_at=now,
    )
    fb_post = MetaPost(
        id="fb_1",
        platform=Platform.FACEBOOK,
        image_ref="/path/to/fb.jpg",
        created_at=now,
    )

    fake.schedule_post(ig_post, page_access_token="token123")
    fake.schedule_post(fb_post, page_access_token="token123")

    ig_posts = fake.list_posts(Platform.INSTAGRAM)
    fb_posts = fake.list_posts(Platform.FACEBOOK)

    assert len(ig_posts) == 1
    assert len(fb_posts) == 1
    assert ig_posts[0].platform == Platform.INSTAGRAM
    assert fb_posts[0].platform == Platform.FACEBOOK


def test_fake_calls_recorded() -> None:
    fake = FakeMeta()
    post = MetaPost(
        id="post_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/image.jpg",
        caption="Test caption",
        hashtags=["#a", "#b"],
        created_at=datetime.now(),
    )

    media_id = fake.schedule_post(post, page_access_token="token123")
    fake.unschedule_post(media_id, page_access_token="token123")

    assert len(fake.calls) == 2
    assert fake.calls[0][0] == "schedule_post"
    assert fake.calls[0][1]["platform"] == Platform.INSTAGRAM
    assert fake.calls[0][1]["hashtag_count"] == 2
    assert fake.calls[1][0] == "unschedule_post"
    assert fake.calls[1][1]["media_id"] == media_id


def test_fake_clear() -> None:
    fake = FakeMeta()
    post = MetaPost(
        id="post_1",
        platform=Platform.INSTAGRAM,
        image_ref="/path/to/image.jpg",
        created_at=datetime.now(),
    )

    fake.schedule_post(post, page_access_token="token123")
    assert len(fake.posts[Platform.INSTAGRAM]) == 1

    fake.clear()
    assert len(fake.posts[Platform.INSTAGRAM]) == 0
    assert len(fake.calls) == 0


# --- MetaTokens models ---


def test_metatokens_creation() -> None:
    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now,
        refresh_at=now + 86400,  # 24h later
        scopes=["instagram_basic_publish", "pages_manage_posts"],
        page_id="123456",
    )
    assert tokens.access_token == "token123"
    assert tokens.page_id == "123456"
    assert len(tokens.scopes) == 2


def test_metatokens_defaults() -> None:
    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now,
        refresh_at=now + 86400,
    )
    assert tokens.page_id is None
    assert tokens.instagram_account_id is None
    assert tokens.scopes == []


# --- MetaAuthStore ---


def test_store_round_trip(tmp_path: Path) -> None:
    store = MetaAuthStore(tmp_path / "tokens.json")
    assert store.load() is None

    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now,
        refresh_at=now + 86400,
        scopes=["instagram_basic_publish"],
        page_id="123456",
    )

    store.save(tokens)
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "token123"
    assert loaded.page_id == "123456"
    assert loaded.scopes == ["instagram_basic_publish"]


def test_store_load_missing_file_returns_none(tmp_path: Path) -> None:
    store = MetaAuthStore(tmp_path / "nope.json")
    assert store.load() is None


def test_store_load_wrong_schema_raises(tmp_path: Path) -> None:
    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text('{"schema": "wrong"}')

    store = MetaAuthStore(tokens_file)
    with pytest.raises(ValueError, match="unexpected Meta token file schema"):
        store.load()


def test_store_save_creates_parent_directories(tmp_path: Path) -> None:
    store = MetaAuthStore(tmp_path / "deep" / "nested" / "tokens.json")
    assert not store.path.parent.exists()

    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now,
        refresh_at=now + 86400,
    )
    store.save(tokens)

    assert store.path.exists()
    assert store.path.parent.exists()


def test_store_save_is_atomic(tmp_path: Path) -> None:
    """Verify that save() uses atomic writes (tmp + replace)."""
    store = MetaAuthStore(tmp_path / "tokens.json")
    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now,
        refresh_at=now + 86400,
    )

    store.save(tokens)

    # Verify no .part file is left behind
    part_file = store.path.with_name(store.path.name + ".part")
    assert not part_file.exists()
    assert store.path.exists()


def test_store_needs_refresh_no_tokens(tmp_path: Path) -> None:
    store = MetaAuthStore(tmp_path / "tokens.json")
    assert not store.needs_refresh()


def test_store_needs_refresh_false_when_too_early(tmp_path: Path) -> None:
    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now,
        refresh_at=now + 3600,  # 1 hour in future
    )

    store = MetaAuthStore(tmp_path / "tokens.json")
    store.save(tokens)

    assert not store.needs_refresh(now=lambda: now)


def test_store_needs_refresh_true_when_ready(tmp_path: Path) -> None:
    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now - 100000,
        refresh_at=now - 3600,  # 1 hour in past
    )

    store = MetaAuthStore(tmp_path / "tokens.json")
    store.save(tokens)

    assert store.needs_refresh(now=lambda: now)


def test_store_token_expires_at(tmp_path: Path) -> None:
    now = time.time()
    issued = now - 86400  # 1 day ago
    tokens = MetaTokens(
        access_token="token123",
        issued_at=issued,
        refresh_at=now,
    )

    store = MetaAuthStore(tmp_path / "tokens.json")
    store.save(tokens)

    expires_at = store.token_expires_at(now=lambda: now)
    assert expires_at is not None
    # Token expires 60 days from issue
    expected_expiry = issued + (60 * 24 * 3600)
    assert expires_at == expected_expiry


def test_store_token_expires_at_no_tokens(tmp_path: Path) -> None:
    store = MetaAuthStore(tmp_path / "tokens.json")
    assert store.token_expires_at() is None


def test_store_schema_round_trip_with_alias(tmp_path: Path) -> None:
    """Verify schema field uses alias correctly in JSON."""
    store = MetaAuthStore(tmp_path / "tokens.json")
    now = time.time()
    tokens = MetaTokens(
        access_token="token123",
        issued_at=now,
        refresh_at=now + 86400,
    )

    store.save(tokens)

    # Read the raw JSON to verify the alias is used
    raw = json.loads(tmp_path.joinpath("tokens.json").read_text())
    assert "schema" in raw  # should use alias, not schema_version
    assert raw["schema"] == "shopsteward.metatokens/1"


# --- Error handling ---


def test_metagraph_error() -> None:
    error = MetaGraphError(
        "Posting failed",
        status_code=429,
        error_code="RATE_LIMIT_EXCEEDED",
    )
    assert error.message == "Posting failed"
    assert error.status_code == 429
    assert error.error_code == "RATE_LIMIT_EXCEEDED"
    assert str(error) == "Posting failed"


def test_metagraph_error_minimal() -> None:
    error = MetaGraphError("Network error")
    assert error.message == "Network error"
    assert error.status_code is None
    assert error.error_code is None
