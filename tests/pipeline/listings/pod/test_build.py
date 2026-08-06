import json

import pytest
from PIL import Image

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1


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


# The shipped catalog's aspect "2:3" (ratio 1.5), landscape orientation --
# every acrylic/poster/canvas variant matches this at a long edge of 6000px
# (well above every shipped variant's 150dpi floor).
_W, _H = 6000, 4000


def test_photo_landed_as_tiff_and_jpeg_produces_one_draft_per_product_type(conn, tmp_path):
    # Gate 2's export preset lands BOTH an AdobeRGB TIFF master and an sRGB
    # JPEG per photo, sharing photo_id but with DIFFERENT file_ids -- this
    # must collapse to ONE draft per surviving product type, not two
    # (design §3, CORRECTED 2026-08-04: keying draft_id on landing_file_id
    # built two Gelato products / two live Etsy listings of the same photo).
    _land(conn, tmp_path, file_id="f-tiff", photo_id="p1", width=_W, height=_H, fmt="TIFF")
    _land(conn, tmp_path, file_id="f-jpeg", photo_id="p1", width=_W, height=_H, fmt="JPEG")

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    assert report.drafts_built == 3  # acrylic, poster, canvas -- NOT 6
    rows = conn.execute(
        "SELECT draft_id, format FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
    ).fetchall()
    assert len(rows) == 3
    assert {r["format"] for r in rows} == {"acrylic", "poster", "canvas"}


def test_draft_id_is_stable_regardless_of_which_sibling_is_encountered_first(conn, tmp_path):
    _land(conn, tmp_path, file_id="f-tiff", photo_id="p1", width=_W, height=_H, fmt="TIFF")
    _land(conn, tmp_path, file_id="f-jpeg", photo_id="p1", width=_W, height=_H, fmt="JPEG")
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    draft_ids_a = {
        r["draft_id"]
        for r in conn.execute(
            "SELECT draft_id FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchall()
    }

    # A second, independent build where the JPEG sibling's file_id sorts
    # BEFORE the TIFF's -- the opposite scan order from above -- must
    # produce the IDENTICAL draft_id set: draft_id keys on photo_id, never
    # on landing_file_id or which row a dict/scan visits first.
    conn2 = connect(tmp_path / "t2.db")
    migrate(conn2)
    _land(conn2, tmp_path, file_id="a-jpeg", photo_id="p1", width=_W, height=_H, fmt="JPEG")
    _land(conn2, tmp_path, file_id="z-tiff", photo_id="p1", width=_W, height=_H, fmt="TIFF")
    build_pod_drafts(conn2, USER_ID, print_file_host=FakePrintFileHost())
    draft_ids_b = {
        r["draft_id"]
        for r in conn2.execute(
            "SELECT draft_id FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchall()
    }

    assert draft_ids_a == draft_ids_b
    assert len(draft_ids_a) == 3


def test_build_creates_one_draft_per_surviving_product_type(conn, tmp_path):
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    # acrylic, poster, canvas all match a 2:3 landscape photo in the shipped
    # catalog; canvas_portrait does not (portrait-only).
    assert report.drafts_built == 3
    assert report.print_files_hosted == 3
    assert report.pod_skipped == 0

    rows = conn.execute(
        "SELECT format, provider, sku_source, listing_type, state, pod_config_hash, "
        "unit_cost, print_file_key, variants_json FROM proj_listing_drafts "
        "WHERE user_id=? ORDER BY format",
        (USER_ID,),
    ).fetchall()
    assert {r["format"] for r in rows} == {"acrylic", "poster", "canvas"}
    for r in rows:
        assert r["provider"] == "gelato"
        assert r["sku_source"] == "provider"
        assert r["listing_type"] == "physical"
        assert r["pod_config_hash"] is not None
        assert r["unit_cost"] is not None
        assert r["print_file_key"] is not None
        variants = json.loads(r["variants_json"])
        assert variants  # every kept size is present
        for v in variants:
            # variants_selected + priced merged into ONE dict per format
            # (design §3), not two separate/clobbering writes.
            assert v["variant_key"]
            assert v["retail_price"] > 0
            assert v["margin_pct"] > 0


def test_acrylic_prices_match_the_shipped_retail_overrides(conn, tmp_path):
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    row = conn.execute(
        "SELECT variants_json FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'",
        (USER_ID,),
    ).fetchone()
    by_format = {v["format"]: v for v in json.loads(row["variants_json"])}
    assert by_format["acrylic_16x24"]["retail_price"] == 149.0
    assert by_format["acrylic_20x30"]["retail_price"] == 179.0
    assert by_format["acrylic_24x36"]["retail_price"] == 229.0


def test_portrait_photo_only_keeps_the_orientation_capable_product_type(conn, tmp_path):
    # The shipped catalog's acrylic/poster/canvas templates are all
    # landscape-only (design §5's confirmed defect fix, slice 1); only
    # canvas_portrait declares portrait variants. A portrait photo of the
    # identical 2:3 ratio must not be side-cropped into a landscape SKU.
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_H, height=_W)  # portrait, same ratio

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    formats = {
        r["format"]
        for r in conn.execute(
            "SELECT format FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchall()
    }
    assert formats == {"canvas_portrait"}
    assert report.drafts_built == 1

    # The orientation-mismatch drops for acrylic/poster/canvas have no
    # surviving draft of their own to attach to -- canvas_portrait's
    # variants_selected event carries the full photo-level dropped[] so the
    # diagnostic isn't lost.
    selected = [e for e in read_all(conn, "listingdraft.variants_selected") if e.user_id == USER_ID]
    assert len(selected) == 1
    dropped_types = {d["product_type"]: d["reason"] for d in selected[0].payload["dropped"]}
    assert dropped_types == {
        "acrylic": "orientation",
        "poster": "orientation",
        "canvas": "orientation",
    }


def test_re_run_is_idempotent_and_hosts_nothing_new(conn, tmp_path):
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    host = FakePrintFileHost()
    build_pod_drafts(conn, USER_ID, print_file_host=host)
    calls_after_first = len(host.calls)

    report = build_pod_drafts(conn, USER_ID, print_file_host=host)

    assert report.drafts_built == 0
    assert report.print_files_hosted == 0
    assert report.skipped_idempotent == 3
    assert len(host.calls) == calls_after_first  # no new publish() calls


def test_pod_skipped_when_no_variant_survives(conn, tmp_path):
    # ratio 1.7 -- outside tolerance of every class the shipped catalog
    # declares (4:5=1.25, 2:3=1.5, 1:1=1.0), so aspect_of itself returns None.
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=1700, height=1000)

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    assert report.drafts_built == 0
    assert report.pod_skipped == 1
    skipped = [e for e in read_all(conn, "listingdraft.pod_skipped") if e.user_id == USER_ID]
    assert len(skipped) == 1
    assert skipped[0].payload["landing_file_id"] == "f1"
    assert skipped[0].payload["reason"] == "aspect"


def test_disabled_config_skips_the_whole_phase(conn, tmp_path):
    from shopsteward.pipeline.listings.pod import config as pod_config
    from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config

    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)

    edited = pod_config.load_pod_config().model_dump(by_alias=True)
    edited["enabled"] = False
    edited_path = tmp_path / "disabled_pod.json"
    edited_path.write_text(json.dumps(edited))
    # apply() (not seed()) writes the disabled config as the seeded row --
    # build_pod_drafts's own internal seed() call is then a no-op (a name
    # already seeded), so the disabled config it reads back is this one.
    pod_config.apply(conn, USER_ID, edited_path)
    rebuild_pod_config(conn)

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    assert report == report.__class__()  # every counter still zero
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchone()["n"]
        == 0
    )


def test_photo_id_filter_limits_to_one_photo(conn, tmp_path):
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    _land(conn, tmp_path, file_id="f2", photo_id="p2", width=_W, height=_H)

    report = build_pod_drafts(conn, USER_ID, photo_id="p1", print_file_host=FakePrintFileHost())

    assert report.drafts_built == 3
    rows = conn.execute(
        "SELECT photo_id FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
    ).fetchall()
    assert {r["photo_id"] for r in rows} == {"p1"}


def test_print_file_hosted_event_never_carries_the_hosted_url(conn, tmp_path):
    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    hosted_events = [
        e for e in read_all(conn, "listingdraft.print_file_hosted") if e.user_id == USER_ID
    ]
    assert hosted_events
    for e in hosted_events:
        assert set(e.payload.keys()) == {"draft_id", "host", "file_key", "expires_at", "sha256"}
        assert "url" not in e.payload


def test_above_max_price_drops_only_the_over_ceiling_size(conn, tmp_path):
    # carry-forward fix (design §13 slice 2 note): above_max_price is
    # reachable for the first time this slice, and only the specific
    # over-ceiling size is dropped -- its cheaper siblings still ship.
    from shopsteward.pipeline.listings.pod import config as pod_config
    from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config

    edited = pod_config.load_pod_config().model_dump(by_alias=True)
    edited["pricing"]["max_price"] = 200.0  # acrylic_24x36 overrides to 229 -- now too expensive
    edited_path = tmp_path / "low_ceiling_pod.json"
    edited_path.write_text(json.dumps(edited))
    pod_config.apply(conn, USER_ID, edited_path)
    rebuild_pod_config(conn)

    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    row = conn.execute(
        "SELECT variants_json FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'",
        (USER_ID,),
    ).fetchone()
    surviving = {v["format"] for v in json.loads(row["variants_json"])}
    assert surviving == {"acrylic_16x24", "acrylic_20x30"}
    assert "acrylic_24x36" not in surviving

    # dropped[] is duplicated verbatim across every draft this photo
    # produces (build.py's design choice, see its comment) -- de-dupe
    # before asserting on the distinct drop this ceiling should produce.
    selected = [e for e in read_all(conn, "listingdraft.variants_selected") if e.user_id == USER_ID]
    acrylic_drops = {
        (d["product_type"], d["format"], d["reason"])
        for e in selected
        for d in e.payload["dropped"]
        if d["product_type"] == "acrylic" and d["reason"] == "above_max_price"
    }
    assert acrylic_drops == {("acrylic", "acrylic_24x36", "above_max_price")}


def test_force_never_re_hosts_a_published_draft(conn, tmp_path):
    from shopsteward.core.events import Event, append

    _land(conn, tmp_path, file_id="f1", photo_id="p1", width=_W, height=_H)
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    rebuild_listings(conn)
    draft_id = conn.execute(
        "SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'", (USER_ID,)
    ).fetchone()["draft_id"]
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="gate3.published",
            payload={"draft_id": draft_id, "published_at": "2026-08-04T00:00:00Z"},
        ),
    )
    rebuild_listings(conn)

    host = FakePrintFileHost()
    report = build_pod_drafts(conn, USER_ID, force=True, print_file_host=host)

    assert report.drafts_built == 2  # poster + canvas rebuilt, acrylic (published) left alone
    assert len(host.calls) == 2  # one re-publish per rebuilt draft -- none for acrylic
