import json

import pytest
from PIL import Image

from shopsteward.adapters.pod.fake import FakeGelatoAdapter
from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
from shopsteward.pipeline.listings.pod.provider import link_pod_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.source_assets import resolve_source
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1
_W, _H = 6000, 4000  # 2:3 landscape -- matches acrylic/poster/canvas (pod test_build.py precedent)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _land(conn, tmp_path, *, file_id, photo_id, width=_W, height=_H, fmt="JPEG"):
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


def _point_archive_at_tmp(conn, tmp_path):
    # A hard guardrail: the archive root must NEVER be the real `data/` in a
    # test -- point it at tmp_path via config, same as pod_config.apply's
    # "disabled config" precedent (test_build.py).
    edited = asset_store_config.load_asset_store_config().model_dump(by_alias=True)
    edited["root"] = str(tmp_path / "archive")
    edited_path = tmp_path / "asset_store.json"
    edited_path.write_text(json.dumps(edited))
    asset_store_config.apply(conn, USER_ID, edited_path)
    rebuild_listings(conn)


def _build_and_link_to_provider(conn, tmp_path, photo_id):
    _point_archive_at_tmp(conn, tmp_path)
    path = _land(conn, tmp_path, file_id=f"f-{photo_id}", photo_id=photo_id)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    cfg = pod_config.get_pod_config(conn, USER_ID)
    link_pod_drafts(
        conn, USER_ID, adapter=FakeGelatoAdapter(), print_file_host=FakePrintFileHost(), cfg=cfg
    )
    rebuild_listings(conn)
    return path


def test_resolver_finds_the_photo_after_landing_file_deleted(conn, tmp_path):
    path = _build_and_link_to_provider(conn, tmp_path, "p1")
    row = conn.execute(
        "SELECT etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'",
        (USER_ID,),
    ).fetchone()
    listing_id = int(row["etsy_listing_id"])

    path.unlink()  # the landing folder is cleared

    ref = resolve_source(conn, USER_ID, listing_id)

    assert ref is not None
    assert ref.photo_id == "p1"
    assert ref.landing_file_id == "f-p1"
    assert ref.on_disk_present is False
    assert ref.archived is True


def test_resolver_returns_none_for_a_listing_with_no_draft_row(conn):
    rebuild_listings(conn)
    assert resolve_source(conn, USER_ID, 999999) is None


def test_resolver_join_bridges_integer_sale_id_and_text_draft_id(conn, tmp_path):
    # proj_listing_drafts.etsy_listing_id is TEXT; a sale's listing_id is
    # INTEGER -- resolve_source must accept the INTEGER form and still match.
    _build_and_link_to_provider(conn, tmp_path, "p2")
    row = conn.execute(
        "SELECT etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'",
        (USER_ID,),
    ).fetchone()
    assert isinstance(row["etsy_listing_id"], str)  # confirms the type seam actually exists

    ref = resolve_source(conn, USER_ID, int(row["etsy_listing_id"]))
    assert ref is not None
    assert ref.photo_id == "p2"
