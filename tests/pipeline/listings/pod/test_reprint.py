"""build_pod_reprint (gap-fill step 1, design 2026-08-11-source-asset-head,
"How gap-fill consumes the head"): builder only -- see pod/build.py's
docstring on the function itself. No live network anywhere in this file
(FakePrintFileHost, tmp_path-rooted asset store)."""

import hashlib
import json

import pytest
from PIL import Image

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import asset_store_config
from shopsteward.pipeline.listings.pod import config as pod_config
from shopsteward.pipeline.listings.pod import printfile
from shopsteward.pipeline.listings.pod.build import build_pod_drafts, build_pod_reprint
from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config
from shopsteward.pipeline.listings.pricing import BelowFloor
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1

# The shipped catalog's "2:3" aspect, landscape orientation -- acrylic,
# poster and canvas all match at this long edge (test_build.py precedent);
# canvas_portrait does not (portrait-only variants), so a plain
# build_pod_drafts here always yields exactly {acrylic, poster, canvas}.
_W, _H = 6000, 4000


@pytest.fixture()
def conn(tmp_path):
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


def _enable_asset_store(conn, tmp_path, root_name="archive"):
    """Points the managed archive at tmp_path (never real `data/`) and
    ensures asset_store.enabled=True (the shipped default)."""
    cfg = asset_store_config.load_asset_store_config().model_dump(by_alias=True)
    cfg["root"] = str(tmp_path / root_name)
    path = tmp_path / f"{root_name}_asset_store.json"
    path.write_text(json.dumps(cfg))
    asset_store_config.apply(conn, USER_ID, path)


def _limit_pod_formats(conn, tmp_path, formats, name="limited_pod.json"):
    """Seeds a pod.json whose formats_by_aspect["2:3"] is narrowed to
    `formats` -- lets a test build only SOME of a landscape photo's eligible
    product types up front, leaving a genuinely different one for reprint to
    build later."""
    edited = pod_config.load_pod_config().model_dump(by_alias=True)
    edited["formats_by_aspect"]["2:3"] = formats
    path = tmp_path / name
    path.write_text(json.dumps(edited))
    pod_config.apply(conn, USER_ID, path)
    rebuild_pod_config(conn)


def test_reprint_after_landing_cleanup_builds_from_the_archive(conn, tmp_path):
    # The reprint-after-cleanup proof: a normal POD build (limited to poster
    # only) archives the master; the landing file is then deleted; a reprint
    # for a DIFFERENT product type (acrylic) must still succeed, sourcing
    # dims + the print master from the archive -- and the master it resolves
    # must be the exact bytes that were archived.
    _enable_asset_store(conn, tmp_path)
    _limit_pod_formats(conn, tmp_path, ["poster"])

    landing_path = _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    assert build_report.drafts_built == 1  # poster only

    archived = conn.execute(
        "SELECT sha256, format FROM proj_asset_store WHERE user_id=? AND photo_id=?",
        (USER_ID, "p1"),
    ).fetchone()
    assert archived is not None

    landing_path.unlink()  # the landing folder is gone -- the whole point
    assert not landing_path.exists()

    full_cfg = pod_config.load_pod_config()  # the UN-limited catalog, acrylic included
    result = build_pod_reprint(
        conn, USER_ID, "p1", "acrylic", print_file_host=FakePrintFileHost(), pod_cfg=full_cfg
    )

    assert result.built is True
    assert result.product_type == "acrylic"
    assert result.draft_id is not None

    draft = conn.execute(
        "SELECT format, photo_id, landing_file_id, state, print_file_key FROM "
        "proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, result.draft_id),
    ).fetchone()
    assert draft is not None
    assert draft["format"] == "acrylic"
    assert draft["photo_id"] == "p1"
    assert draft["print_file_key"] is not None  # reached print_file_hosted

    # The print master resolve_print_source_path recovers (with the landing
    # file gone) comes from the archive and matches the archived original's
    # sha256 -- a corrupt/mismatched fallback would raise ValueError instead.
    resolved_path = printfile.resolve_print_source_path(
        conn, USER_ID, draft["landing_file_id"], full_cfg.print_file.prefer
    )
    with open(resolved_path, "rb") as fh:
        resolved_sha256 = hashlib.sha256(fh.read()).hexdigest()
    assert resolved_sha256 == archived["sha256"]

    # New draft_id must not collide with the pre-existing poster draft.
    poster_draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND format='poster'", (USER_ID,)
    ).fetchone()["draft_id"]
    assert poster_draft_id != result.draft_id


def test_reprint_draft_is_shape_identical_to_a_normal_pod_draft(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)

    # Photo A: a normal, full build -- acrylic comes out the ordinary way.
    _land(conn, tmp_path, file_id="fa", photo_id="pa", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    normal_draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND photo_id='pa' "
        "AND format='acrylic'",
        (USER_ID,),
    ).fetchone()["draft_id"]

    # Photo B: limited to poster up front, then reprinted into acrylic after
    # its landing file is deleted.
    _limit_pod_formats(conn, tmp_path, ["poster"])
    landing_b = _land(conn, tmp_path, file_id="fb", photo_id="pb", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    landing_b.unlink()
    full_cfg = pod_config.load_pod_config()
    reprint_result = build_pod_reprint(
        conn, USER_ID, "pb", "acrylic", print_file_host=FakePrintFileHost(), pod_cfg=full_cfg
    )
    assert reprint_result.built is True

    def _event_shape(draft_id):
        events = [
            e
            for e in read_all(conn)
            if e.user_id == USER_ID and e.payload.get("draft_id") == draft_id
        ]
        return {e.type: set(e.payload.keys()) for e in events}

    normal_shape = _event_shape(normal_draft_id)
    reprint_shape = _event_shape(reprint_result.draft_id)

    assert (
        set(normal_shape)
        == set(reprint_shape)
        == {
            "listingdraft.created",
            "listingdraft.variants_selected",
            "listingdraft.priced",
            "listingdraft.print_file_prepared",
            "listingdraft.print_file_hosted",
        }
    )
    for event_type in normal_shape:
        assert normal_shape[event_type] == reprint_shape[event_type]

    # And the resulting proj_listing_drafts rows share the same shape too
    # (same non-null columns) -- link/enrich/push read this table, not the
    # event log, so this is what actually has to be indistinguishable.
    cols = "state, print_file_key, provider, listing_type, sku_source, pod_config_hash"
    normal_row = dict(
        conn.execute(
            f"SELECT {cols} FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
            (USER_ID, normal_draft_id),
        ).fetchone()
    )
    reprint_row = dict(
        conn.execute(
            f"SELECT {cols} FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
            (USER_ID, reprint_result.draft_id),
        ).fetchone()
    )
    assert {k: v is None for k, v in normal_row.items()} == {
        k: v is None for k, v in reprint_row.items()
    }
    assert normal_row["state"] == reprint_row["state"] == "built"
    assert normal_row["provider"] == reprint_row["provider"]


def test_not_archived_is_a_no_op_not_an_exception(conn):
    result = build_pod_reprint(
        conn, USER_ID, "never-archived", "acrylic", print_file_host=FakePrintFileHost()
    )
    assert result == result.__class__(built=False, reason="not_archived")
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchone()["n"]
        == 0
    )


def test_unknown_type_is_a_no_op(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    result = build_pod_reprint(
        conn, USER_ID, "p1", "not_a_real_product_type", print_file_host=FakePrintFileHost()
    )
    assert result == result.__class__(built=False, reason="unknown_type")


def test_already_exists_is_a_no_op_and_never_duplicates(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    existing_draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'", (USER_ID,)
    ).fetchone()["draft_id"]

    result = build_pod_reprint(conn, USER_ID, "p1", "acrylic", print_file_host=FakePrintFileHost())

    assert result.built is False
    assert result.reason == "already_exists"
    assert result.draft_id == existing_draft_id
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'",
        (USER_ID,),
    ).fetchone()["n"]
    assert rows == 1  # never a duplicate draft


def test_reprint_applies_the_same_margin_floor_as_the_normal_path(conn, tmp_path):
    # A retail_override set below both margin floors must fail LOUDLY
    # (BelowFloor) via build_pod_reprint exactly as it would via
    # build_pod_drafts -- proves _select_and_price/_price_variant are reused
    # verbatim, not reimplemented.
    _enable_asset_store(conn, tmp_path)
    _limit_pod_formats(conn, tmp_path, ["poster"])
    landing_path = _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    landing_path.unlink()

    edited = pod_config.load_pod_config().model_dump(by_alias=True)
    edited["catalog"]["gelato"]["products"]["acrylic"]["variants"][0]["retail_override"] = 1.0
    from shopsteward.pipeline.listings.pod.models import PodConfig

    below_floor_cfg = PodConfig.model_validate(edited)

    with pytest.raises(BelowFloor):
        build_pod_reprint(
            conn,
            USER_ID,
            "p1",
            "acrylic",
            print_file_host=FakePrintFileHost(),
            pod_cfg=below_floor_cfg,
        )
