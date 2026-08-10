import json

import pytest
from PIL import Image

from shopsteward.adapters.pod.fake import FakeGelatoAdapter
from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
from shopsteward.pipeline.listings.pod.provider import link_pod_drafts
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # store_id now resolves from GELATO_STORE_ID (pod.json ships only a
    # placeholder, rejected by PodProviderRef); set a real one so acrylic/
    # poster specs validate. canvas still fails on its "<OPERATOR>" template_id.
    monkeypatch.setenv("GELATO_STORE_ID", "test-store")
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _land(conn, tmp_path, *, file_id, photo_id, width, height, fmt="JPEG"):
    path = tmp_path / f"{file_id}.jpg"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(path, "JPEG")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": file_id,
                "path": str(path),
                "base_name": file_id,
                "format": fmt,
                "width": width,
                "height": height,
                "color_space": "sRGB",
                "photo_id": photo_id,
            },
        ),
    )
    rebuild_pipeline(conn)
    return path


# The shipped catalog's "2:3" aspect, landscape orientation -- matches
# acrylic/poster/canvas (test_build.py precedent). canvas's shipped
# template_id is still the "<OPERATOR>" placeholder (pod.json), so every
# build here always produces exactly one draft link_pod_drafts CANNOT
# succeed on -- useful for proving a per-draft failure never stops the rest.
_W, _H = 6000, 4000


def _seed_hosted_drafts(conn, tmp_path, photo_id):
    _land(conn, tmp_path, file_id=f"f-{photo_id}", photo_id=photo_id, width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())


def _row(conn, product_type, photo_id):
    return conn.execute(
        "SELECT * FROM proj_listing_drafts WHERE user_id=? AND format=? AND photo_id=?",
        (USER_ID, product_type, photo_id),
    ).fetchone()


def test_create_poll_link_succeeds_and_never_leaks_print_file_url(conn, tmp_path):
    _seed_hosted_drafts(conn, tmp_path, "p1")
    cfg = pod_config.get_pod_config(conn, USER_ID)
    adapter = FakeGelatoAdapter()  # default links_after_polls=2
    host = FakePrintFileHost()

    report = link_pod_drafts(conn, USER_ID, adapter=adapter, print_file_host=host, cfg=cfg)

    # acrylic + poster have real catalog identity and link cleanly; canvas's
    # shipped template_id is still "<OPERATOR>" and fails spec construction.
    assert report.created == 2
    assert report.linked == 2
    assert report.failed == 1

    acrylic = _row(conn, "acrylic", "p1")
    assert acrylic["pod_status"] == "linked"
    assert acrylic["provider_product_id"] is not None
    assert acrylic["etsy_listing_id"] is not None

    events = read_all(conn, "listingdraft.provider_")
    acrylic_events = [e.type for e in events if e.payload["draft_id"] == acrylic["draft_id"]]
    assert acrylic_events == ["listingdraft.provider_created", "listingdraft.provider_linked"]

    # provider_created must record variant_count, never a print_file_url.
    created_payload = next(
        e.payload for e in events if e.type == "listingdraft.provider_created"
        and e.payload["draft_id"] == acrylic["draft_id"]
    )
    assert "print_file_url" not in created_payload
    assert created_payload["variant_count"] > 0

    # No emitted event anywhere carries the fake host's URL (grep every
    # payload for its scheme -- the fake always mints "https://fake.invalid/<key>").
    for e in events:
        assert "fake.invalid" not in json.dumps(e.payload)


def test_second_run_is_idempotent_and_creates_nothing_new(conn, tmp_path):
    _seed_hosted_drafts(conn, tmp_path, "p1")
    cfg = pod_config.get_pod_config(conn, USER_ID)
    adapter = FakeGelatoAdapter()
    host = FakePrintFileHost()

    link_pod_drafts(conn, USER_ID, adapter=adapter, print_file_host=host, cfg=cfg)
    calls_after_first_run = list(adapter.calls)

    report2 = link_pod_drafts(conn, USER_ID, adapter=adapter, print_file_host=host, cfg=cfg)

    # acrylic + poster are already confirmed linked -> skipped outright;
    # canvas has no provider_product_id (its spec never validated) so it is
    # retried and fails again, but NEVER reaches the adapter.
    assert report2.skipped_idempotent == 2
    assert report2.created == 0
    assert report2.linked == 0
    assert adapter.calls == calls_after_first_run  # no new create_product/get_product calls


def test_poll_exhaustion_fails_without_raising_and_other_drafts_still_process(conn, tmp_path):
    _seed_hosted_drafts(conn, tmp_path, "p1")
    _seed_hosted_drafts(conn, tmp_path, "p2")
    cfg = pod_config.get_pod_config(conn, USER_ID)
    adapter = FakeGelatoAdapter(links_after_polls=999)  # never links within poll_max
    host = FakePrintFileHost()

    report = link_pod_drafts(
        conn, USER_ID, adapter=adapter, print_file_host=host, cfg=cfg, poll_max=2
    )

    assert report.linked == 0
    assert report.failed > 0

    for photo_id in ("p1", "p2"):
        acrylic = _row(conn, "acrylic", photo_id)
        assert acrylic["pod_status"] == "failed"
        assert acrylic["provider_product_id"] is not None  # create succeeded, only linking failed

        events = [
            e for e in read_all(conn, "listingdraft.provider_")
            if e.payload["draft_id"] == acrylic["draft_id"]
        ]
        assert [e.type for e in events] == [
            "listingdraft.provider_created",
            "listingdraft.provider_failed",
        ]
        assert events[-1].payload["reason"] == "poll_exhausted"
