import pytest

from shopsteward.adapters.copy.fake import FakeCopyAdapter, FixtureCopyAdapter
from shopsteward.adapters.copy.interface import CopyResult, CopyUsage, CopyVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.copy import build_copy_adapter, generate_copy
from shopsteward.pipeline.listings.models import ListingImage
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1
DRAFT_ID = "draft-1"
LANDING_FILE_ID = "f" * 64
PHOTO_ID = "photo-1"

IMAGE = ListingImage(path="/mockups/single.jpg", intent="single", rank=1)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


@pytest.fixture()
def cfg(conn):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    return listing_config.get_config(conn, USER_ID)


def _seed_landscape_landing_file(conn):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": LANDING_FILE_ID,
                "path": "/landing/photo.jpg",
                "base_name": None,
                "format": "JPEG",
                "width": 4000,
                "height": 3000,
                "color_space": "sRGB",
                "photo_id": PHOTO_ID,
            },
        ),
    )
    rebuild_pipeline(conn)


def _seed_score(conn, *, subject="osprey", strongest_room_style="coastal", one_risk="glare"):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="photo.scored",
            payload={
                "photo_id": PHOTO_ID,
                "profile_name": "default",
                "scores": {"technical": 80, "commercial": 70},
                "composite": 75,
                "escalated": False,
                "vision": {
                    "triage": {
                        "model": "google/gemini-2.5-flash-lite",
                        "verdict": {
                            "commercial_score": 70,
                            "subject": subject,
                            "strongest_room_style": strongest_room_style,
                            "one_risk": one_risk,
                            "rationale": "well composed",
                        },
                    }
                },
            },
        ),
    )
    rebuild_pipeline(conn)


def test_generate_copy_appends_event_and_disclosure_when_carrying_mockups(conn, cfg):
    _seed_landscape_landing_file(conn)
    _seed_score(conn)

    ok = generate_copy(
        conn,
        USER_ID,
        DRAFT_ID,
        LANDING_FILE_ID,
        PHOTO_ID,
        [IMAGE],
        FixtureCopyAdapter(),
        cfg,
        live=False,
        soft_cap_usd=10.0,
    )
    assert ok is True

    events = [e for e in read_all(conn, "listingdraft.copy_generated") if e.user_id == USER_ID]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["draft_id"] == DRAFT_ID
    assert payload["disclosure_appended"] is True
    disclosure = (
        "Room scenes are AI-generated staging mockups. The photograph itself is the "
        "artist's original work and is never AI-generated or AI-edited."
    )
    assert disclosure in payload["description"]
    # fixture adapter -> usage None -> no llm.call event
    assert [e for e in read_all(conn, "llm.call") if e.user_id == USER_ID] == []


def test_generate_copy_without_images_skips_disclosure(conn, cfg):
    _seed_landscape_landing_file(conn)
    _seed_score(conn)

    generate_copy(
        conn,
        USER_ID,
        DRAFT_ID,
        LANDING_FILE_ID,
        PHOTO_ID,
        [],  # no mockups on this (hypothetical) draft
        FixtureCopyAdapter(),
        cfg,
        live=False,
        soft_cap_usd=10.0,
    )

    payload = read_all(conn, "listingdraft.copy_generated")[0].payload
    assert payload["disclosure_appended"] is False
    assert "Room scenes are AI-generated" not in payload["description"]


def test_generate_copy_reads_vision_verdict_from_proj_scores(conn, cfg):
    _seed_landscape_landing_file(conn)
    _seed_score(conn, subject="bear cub", strongest_room_style="rustic cabin")

    adapter = FakeCopyAdapter(
        [CopyResult(verdict=CopyVerdict(title="t", tags=["a"] * 13, description="d"), usage=None)]
    )
    generate_copy(
        conn,
        USER_ID,
        DRAFT_ID,
        LANDING_FILE_ID,
        PHOTO_ID,
        [IMAGE],
        adapter,
        cfg,
        live=False,
        soft_cap_usd=10.0,
    )

    inputs, model = adapter.calls[0]
    assert inputs.subject == "bear cub"
    assert inputs.strongest_room_style == "rustic cabin"
    assert inputs.one_risk == "glare"
    assert inputs.orientation == "landscape"
    assert inputs.sizes  # from mockups.json whatyougot
    assert model == cfg.copy_.model


def test_generate_copy_without_a_score_row_leaves_signals_none(conn, cfg):
    _seed_landscape_landing_file(conn)
    # no photo.scored event this time

    adapter = FakeCopyAdapter(
        [CopyResult(verdict=CopyVerdict(title="t", tags=["a"] * 13, description="d"), usage=None)]
    )
    generate_copy(
        conn,
        USER_ID,
        DRAFT_ID,
        LANDING_FILE_ID,
        PHOTO_ID,
        [IMAGE],
        adapter,
        cfg,
        live=False,
        soft_cap_usd=10.0,
    )

    inputs, _ = adapter.calls[0]
    assert inputs.subject is None
    assert inputs.strongest_room_style is None


def test_generate_copy_appends_llm_call_when_usage_present(conn, cfg):
    _seed_landscape_landing_file(conn)
    _seed_score(conn)

    verdict = CopyVerdict(title="t", tags=["a"] * 13, description="d")
    usage = CopyUsage(
        model="anthropic/claude-sonnet-5", input_tokens=500, output_tokens=200, est_cost_usd=0.003
    )
    adapter = FakeCopyAdapter([CopyResult(verdict=verdict, usage=usage)])

    generate_copy(
        conn,
        USER_ID,
        DRAFT_ID,
        LANDING_FILE_ID,
        PHOTO_ID,
        [IMAGE],
        adapter,
        cfg,
        live=True,
        soft_cap_usd=10.0,
    )

    calls = [e for e in read_all(conn, "llm.call") if e.user_id == USER_ID]
    assert len(calls) == 1
    assert calls[0].payload["purpose"] == "listing_copy"
    assert calls[0].payload["draft_id"] == DRAFT_ID
    assert calls[0].payload["est_cost_usd"] == 0.003


def test_generate_copy_refuses_when_soft_cap_reached(conn, cfg):
    _seed_landscape_landing_file(conn)
    _seed_score(conn)

    # plant enough spend this month to already be at the cap
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="llm.call",
            payload={
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-5",
                "purpose": "commercial_triage",
                "photo_id": "other",
                "input_tokens": 1,
                "output_tokens": 1,
                "est_cost_usd": 10.0,
            },
        ),
    )

    adapter = FakeCopyAdapter([])  # exhausted immediately -> would raise if called
    ok = generate_copy(
        conn,
        USER_ID,
        DRAFT_ID,
        LANDING_FILE_ID,
        PHOTO_ID,
        [IMAGE],
        adapter,
        cfg,
        live=True,
        soft_cap_usd=10.0,
    )

    assert ok is False
    assert adapter.calls == []
    assert [e for e in read_all(conn, "listingdraft.copy_generated") if e.user_id == USER_ID] == []


def test_generate_copy_soft_cap_does_not_apply_offline(conn, cfg):
    _seed_landscape_landing_file(conn)
    _seed_score(conn)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="llm.call",
            payload={
                "provider": "openrouter",
                "model": "m",
                "purpose": "commercial_triage",
                "photo_id": "other",
                "input_tokens": 1,
                "output_tokens": 1,
                "est_cost_usd": 10.0,
            },
        ),
    )

    ok = generate_copy(
        conn,
        USER_ID,
        DRAFT_ID,
        LANDING_FILE_ID,
        PHOTO_ID,
        [IMAGE],
        FixtureCopyAdapter(),
        cfg,
        live=False,
        soft_cap_usd=10.0,
    )
    assert ok is True


def test_build_copy_adapter_offline_returns_fixture(cfg):
    adapter = build_copy_adapter(cfg, live=False)
    assert isinstance(adapter, FixtureCopyAdapter)


def test_build_copy_adapter_live_requires_env_key(cfg, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(KeyError):
        build_copy_adapter(cfg, live=True)


def test_build_copy_adapter_live_reads_prompt_template_and_config(cfg, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    adapter = build_copy_adapter(cfg, live=True)
    assert adapter._prompt_template  # loaded from listing.json copy.prompt_path
    assert adapter._temperature == cfg.copy_.temperature
