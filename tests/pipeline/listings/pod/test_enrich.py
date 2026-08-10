"""Tests for pod/enrich.py (Phase C, slice 4): copy + images pushed onto an
ALREADY-LINKED (Gelato-created) Etsy draft. The Etsy listing itself is never
created by this module -- it exists on the provider's side before enrichment
ever runs -- so the test seeds FakeEtsyWriteAdapter.listings directly
(a public attribute the fake exposes precisely for this: see FakeEtsyWriteAdapter's
docstring, "`calls` records every method invocation... for assertions") rather
than routing through create_draft_listing, mirroring how a POD draft's
etsy_listing_id really gets populated (listingdraft.provider_linked, not
listingdraft.pushed_to_etsy)."""

import pytest
from PIL import Image

from shopsteward.adapters.copy.fake import FixtureCopyAdapter
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.pod.enrich import enrich_pod_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline
from tests.pipeline.listings.helpers import seed_landing_file_with_mockup_set

USER_ID = 1
LISTING_ID = 5001
FILE_ID = "f" * 64
PHOTO_ID = "photo-trail"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_score(conn, *, subject="trail runner"):
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
                            "strongest_room_style": "rustic",
                            "one_risk": "motion blur",
                            "rationale": "well composed",
                        },
                    }
                },
            },
        ),
    )
    rebuild_pipeline(conn)


def _seed_linked_pod_draft(conn, tmp_path, *, draft_id="pod-draft-1") -> str:
    """Seeds a provider_linked POD draft (mirrors what pod/build.py ->
    pod/provider.py would have already produced by the time enrich runs):
    a landing file with a completed mockup set, a scored photo (vision
    signals for copy), and proj_listing_drafts.pod_status='linked' with
    etsy_listing_id=LISTING_ID."""
    seed_landing_file_with_mockup_set(
        conn,
        file_id=FILE_ID,
        photo_id=PHOTO_ID,
        path=str(tmp_path / "photo.jpg"),
        set_key="set-1",
        intents=["single", "digital_whatyougot"],
        mockups_dir=tmp_path / "mockups",
        user_id=USER_ID,
    )
    Image.new("RGB", (100, 100), (1, 2, 3)).save(tmp_path / "photo.jpg", "JPEG")
    _seed_score(conn, subject="trail runner")

    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)

    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.created",
            payload={
                "draft_id": draft_id,
                "landing_file_id": FILE_ID,
                "photo_id": PHOTO_ID,
                "set_key": None,
                "provider": "gelato",
                "format": "acrylic",
                "sku_source": "provider",
                "listing_type": "physical",
                "config_hash": None,
                "pod_config_hash": "pod-cfg-hash-1",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": LISTING_ID,
                "etsy_listing_state": "draft",
            },
        ),
    )
    rebuild_listings(conn)
    return draft_id


def _adapter_with_existing_listing() -> FakeEtsyWriteAdapter:
    adapter = FakeEtsyWriteAdapter()
    # The Etsy draft already exists (Gelato created it) -- enrich.py must
    # never call create_draft_listing, only update/upload against this id.
    adapter.listings[LISTING_ID] = {
        "title": "placeholder",
        "description": "placeholder",
        "price": 42.0,
        "quantity": 1,
        "tags": [],
        "state": "draft",
        "images": [],
        "files": [],
    }
    return adapter


def test_enriches_linked_draft_with_copy_reflecting_vision_subject_and_uploads_images(
    conn, tmp_path
):
    draft_id = _seed_linked_pod_draft(conn, tmp_path)
    cfg = listing_config.get_config(conn, USER_ID)
    adapter = _adapter_with_existing_listing()
    copy_adapter = FixtureCopyAdapter()

    report = enrich_pod_drafts(
        conn,
        USER_ID,
        etsy_adapter=adapter,
        copy_adapter=copy_adapter,
        cfg=cfg,
        live=False,
        soft_cap_usd=1000.0,
    )

    assert report.enriched == 1
    assert report.failed == 0

    # FixtureCopyAdapter titles as f"{subject.title()} Wall Art, ..." -- the
    # vision "subject" signal from proj_scores reached CopyInputs.
    update_calls = [c for c in adapter.calls if c[0] == "update_listing"]
    assert len(update_calls) == 1
    assert update_calls[0][1]["listing_id"] == LISTING_ID
    assert "Trail Runner" in update_calls[0][1]["fields"]["title"]

    # Never touches price -- EtsyListingUpdate has no price field, and
    # update_listing_price is never called for a POD draft.
    assert all(c[0] != "update_listing_price" for c in adapter.calls)
    assert adapter.listings[LISTING_ID]["price"] == 42.0

    upload_calls = [c for c in adapter.calls if c[0] == "upload_listing_image"]
    assert len(upload_calls) == 2  # single + digital_whatyougot mockups
    assert {c[1]["listing_id"] for c in upload_calls} == {LISTING_ID}

    row = conn.execute(
        "SELECT pod_status, title FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["pod_status"] == "enriched"
    assert "Trail Runner" in row["title"]

    events = [
        e for e in read_all(conn, "listingdraft.enriched") if e.payload["draft_id"] == draft_id
    ]
    assert len(events) == 1
    assert events[0].payload["etsy_listing_id"] == LISTING_ID


def test_second_run_is_idempotent_and_makes_no_further_adapter_calls(conn, tmp_path):
    _seed_linked_pod_draft(conn, tmp_path)
    cfg = listing_config.get_config(conn, USER_ID)
    adapter = _adapter_with_existing_listing()
    copy_adapter = FixtureCopyAdapter()

    enrich_pod_drafts(
        conn, USER_ID, etsy_adapter=adapter, copy_adapter=copy_adapter, cfg=cfg, soft_cap_usd=1000.0
    )
    calls_after_first_run = list(adapter.calls)
    assert calls_after_first_run  # sanity: the first run did call the adapter

    report2 = enrich_pod_drafts(
        conn, USER_ID, etsy_adapter=adapter, copy_adapter=copy_adapter, cfg=cfg, soft_cap_usd=1000.0
    )

    assert report2.enriched == 0
    assert report2.failed == 0
    assert report2.skipped == 0
    assert adapter.calls == calls_after_first_run  # no re-upload, no re-update


def test_skips_draft_with_no_completed_mockup_set_yet(conn, tmp_path):
    # A landing file + a linked POD draft, but NO mockup set at all -- the
    # digital-equivalent "not ready yet" state build_drafts also skips
    # rather than errors.
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": FILE_ID,
                "path": str(tmp_path / "photo.jpg"),
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
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)

    draft_id = "pod-draft-nomockup"
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.created",
            payload={
                "draft_id": draft_id,
                "landing_file_id": FILE_ID,
                "photo_id": PHOTO_ID,
                "set_key": None,
                "provider": "gelato",
                "format": "acrylic",
                "sku_source": "provider",
                "listing_type": "physical",
                "config_hash": None,
                "pod_config_hash": "pod-cfg-hash-1",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": LISTING_ID,
                "etsy_listing_state": "draft",
            },
        ),
    )
    rebuild_listings(conn)

    cfg = listing_config.get_config(conn, USER_ID)
    adapter = _adapter_with_existing_listing()
    copy_adapter = FixtureCopyAdapter()

    report = enrich_pod_drafts(
        conn,
        USER_ID,
        etsy_adapter=adapter,
        copy_adapter=copy_adapter,
        cfg=cfg,
        soft_cap_usd=1000.0,
    )

    assert report.enriched == 0
    assert report.skipped == 1
    assert report.failed == 0
    assert adapter.calls == []  # never touched the Etsy draft

    row = conn.execute(
        "SELECT pod_status FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["pod_status"] == "linked"  # unchanged -- retried next run
