import json

import pytest
from PIL import Image
from pydantic import ValidationError
from typer.testing import CliRunner

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings import crop_focus
from shopsteward.pipeline.listings.projections import rebuild_listings

from .helpers import seed_fully_built_draft

USER_ID = 1
FID = "f" * 64
runner = CliRunner()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_old_config(conn):
    """A pre-slice-2 stored config: no digital_files key at all."""
    raw = json.loads(listing_config.LISTING_CONFIG_PATH.read_text("utf-8"))
    del raw["digital_files"]
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingconfig.seeded",
            payload={"name": "default", "config": raw, "source": "defaults"},
        ),
    )
    rebuild_listings(conn)


def _updated_count(conn):
    return len(conn.execute("SELECT 1 FROM events WHERE type='listingconfig.updated'").fetchall())


def test_fresh_seed_populates_digital_files_inertly(conn):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, USER_ID)
    assert cfg.digital_files is not None
    assert cfg.digital_files.copyright_holder == "PhotosByEricD"


def test_pre_slice2_stored_config_loads_with_none(conn):
    _seed_old_config(conn)
    assert listing_config.get_config(conn, USER_ID).digital_files is None


def test_sync_defaults_adds_only_missing_and_is_idempotent(conn):
    _seed_old_config(conn)
    # an operator edit of an existing key must survive the sync
    listing_config.set_value(conn, USER_ID, "image_cap", 7)
    rebuild_listings(conn)
    assert listing_config.sync_defaults(conn, USER_ID) == ["digital_files"]
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, USER_ID)
    assert cfg.digital_files is not None
    assert cfg.image_cap == 7
    n = _updated_count(conn)
    assert listing_config.sync_defaults(conn, USER_ID) == []
    assert _updated_count(conn) == n


def test_set_nested_null_and_whole_object(conn):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    listing_config.set_value(conn, USER_ID, "digital_files.copyright_holder", "Someone")
    rebuild_listings(conn)
    assert listing_config.get_config(conn, USER_ID).digital_files.copyright_holder == "Someone"
    listing_config.set_value(conn, USER_ID, "digital_files", None)
    rebuild_listings(conn)
    assert listing_config.get_config(conn, USER_ID).digital_files is None
    ev = conn.execute(
        "SELECT user_id FROM events WHERE type='listingconfig.updated' ORDER BY id"
    ).fetchall()
    assert [r["user_id"] for r in ev] == [USER_ID, USER_ID]


@pytest.mark.parametrize(
    ("key", "value"),
    [("image_cap", "ten"), ("digital_files.ppi", "x"), ("nope", 1), ("etsy.nope", 1)],
)
def test_set_invalid_rejected_without_event(conn, key, value):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    with pytest.raises(ValueError):
        listing_config.set_value(conn, USER_ID, key, value)
    assert _updated_count(conn) == 0


def test_cropfocus_last_write_wins_clear_and_rebuild(conn):
    crop_focus.set_crop_focus(conn, USER_ID, FID, 0.1, 0.2)
    crop_focus.set_crop_focus(conn, USER_ID, FID, 0.3, 0.9)
    rebuild_listings(conn)
    rebuild_listings(conn)  # idempotent
    assert crop_focus.get_crop_focus(conn, USER_ID, FID) == (0.3, 0.9)
    assert crop_focus.get_crop_focus(conn, USER_ID, "other") is None
    crop_focus.clear_crop_focus(conn, USER_ID, FID)
    rebuild_listings(conn)
    assert crop_focus.get_crop_focus(conn, USER_ID, FID) is None


@pytest.mark.parametrize(("x", "y"), [(-0.1, 0.5), (0.5, 1.01)])
def test_cropfocus_out_of_range_rejected(conn, x, y):
    with pytest.raises(ValidationError):
        crop_focus.set_crop_focus(conn, USER_ID, FID, x, y)
    assert conn.execute("SELECT 1 FROM events WHERE type LIKE 'cropfocus.%'").fetchone() is None


def test_cli_config_set_and_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    r = runner.invoke(app, ["listings", "config", "set", "digital_files.copyright_holder", '"Zed"'])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["listings", "config", "set", "image_cap", '"bad"'])
    assert r.exit_code == 1
    r = runner.invoke(app, ["listings", "config", "set", "image_cap", "{oops"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["listings", "config", "sync-defaults"])
    assert r.exit_code == 0
    assert "nothing missing" in r.output


def _seed_landing(tmp_path, size=(600, 400)):
    db = tmp_path / "t.db"
    c = connect(db)
    migrate(c)
    path = tmp_path / "p.jpg"
    Image.new("RGB", size, (50, 60, 70)).save(path, "JPEG")
    append(
        c,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": FID,
                "path": str(path),
                "base_name": None,
                "format": "JPEG",
                "width": size[0],
                "height": size[1],
                "color_space": "sRGB",
                "photo_id": "ph",
            },
        ),
    )
    return c


def test_cli_crop_focus_and_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    _seed_landing(tmp_path).close()

    r = runner.invoke(app, ["listings", "crop-focus", FID, "--x", "0", "--y", "0.5"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["listings", "crop-focus", FID, "--x", "2", "--y", "0.5"])
    assert r.exit_code == 1
    r = runner.invoke(app, ["listings", "crop-focus", FID])
    assert r.exit_code == 1

    out = tmp_path / "out"
    r = runner.invoke(app, ["listings", "crop-preview", FID, "--out", str(out)])
    assert r.exit_code == 0, r.output
    # 600x400 is far below 300-ppi print sizes: only the always-shipped
    # `full` entry survives, with no deliverable sizes.
    assert [p.name for p in out.iterdir()] == ["full_nosize.jpg"]

    r = runner.invoke(app, ["listings", "crop-focus", FID, "--clear"])
    assert r.exit_code == 0
    c = connect(tmp_path / "t.db")
    rebuild_listings(c)
    assert crop_focus.get_crop_focus(c, USER_ID, FID) is None


def test_cli_preview_names_and_downscale(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    c = _seed_landing(tmp_path, size=(6000, 4000))
    c.close()
    out = tmp_path / "out"
    r = runner.invoke(app, ["listings", "crop-preview", FID, "--out", str(out)])
    assert r.exit_code == 0, r.output
    names = sorted(p.name for p in out.iterdir())
    assert names == [
        "11x14_11x14.jpg",
        "4x5_4x5-8x10.jpg",
        "5x7_5x7.jpg",
        "A_A4-A3.jpg",
        "full_4x6-8x12-12x18.jpg",
    ]
    for p in out.iterdir():
        with Image.open(p) as im:
            assert max(im.size) <= 1200


def test_cli_listing_id_resolution_and_pushed_warning(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    c = connect(db)
    migrate(c)
    seed_fully_built_draft(c, tmp_path=tmp_path, file_id=FID, photo_id="ph", title="T", set_key="s")
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter, push_drafts

    rebuild_listings(c)
    cfg = listing_config.get_config(c, USER_ID)
    push_drafts(c, USER_ID, cfg, build_etsy_write_adapter(live=False))
    rebuild_listings(c)
    row = c.execute("SELECT etsy_listing_id FROM proj_listing_drafts").fetchone()
    lid = int(row["etsy_listing_id"])
    c.close()

    r = runner.invoke(
        app, ["listings", "crop-focus", "--listing-id", str(lid), "--x", "0.2", "--y", "0.2"]
    )
    assert r.exit_code == 0, r.output
    assert "already pushed" in r.output
    r = runner.invoke(app, ["listings", "crop-focus", "--listing-id", "999", "--clear"])
    assert r.exit_code == 1
    out = tmp_path / "o"
    r = runner.invoke(
        app, ["listings", "crop-preview", "--listing-id", str(lid), "--out", str(out)]
    )
    assert r.exit_code == 0, r.output
    assert any(out.iterdir())


def _canonical_hash(raw: dict) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _raw_defaults_without_digital_files() -> dict:
    raw = json.loads(listing_config.LISTING_CONFIG_PATH.read_text("utf-8"))
    del raw["digital_files"]
    return raw


def test_config_hash_unchanged_for_pre_slice2_config(conn):
    _seed_old_config(conn)
    cfg = listing_config.get_config(conn, USER_ID)
    assert listing_config.config_hash(cfg) == _canonical_hash(_raw_defaults_without_digital_files())


def test_sync_defaults_and_digital_files_set_keep_hash(conn):
    _seed_old_config(conn)
    h0 = listing_config.config_hash(listing_config.get_config(conn, USER_ID))
    listing_config.sync_defaults(conn, USER_ID)
    rebuild_listings(conn)
    assert listing_config.config_hash(listing_config.get_config(conn, USER_ID)) == h0
    listing_config.set_value(conn, USER_ID, "digital_files.copyright_holder", "Q")
    listing_config.set_value(conn, USER_ID, "digital_files", None)
    rebuild_listings(conn)
    assert listing_config.config_hash(listing_config.get_config(conn, USER_ID)) == h0


@pytest.mark.parametrize("key", ["name", "schema"])
def test_set_identity_keys_refused(conn, key):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    with pytest.raises(ValueError, match="identity"):
        listing_config.set_value(conn, USER_ID, key, "x")
    assert _updated_count(conn) == 0


def test_config_set_warns_only_when_hash_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    r = runner.invoke(app, ["listings", "config", "set", "image_cap", "7"])
    assert r.exit_code == 0 and "re-drafted" in r.output
    r = runner.invoke(app, ["listings", "config", "set", "digital_files.ppi", "250"])
    assert r.exit_code == 0 and "re-drafted" not in r.output


def test_build_skips_photo_already_pushed_under_earlier_config(conn, tmp_path):
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter, push_drafts

    from .helpers import seed_landing_file_with_mockup_set

    photo = tmp_path / "ph.jpg"
    Image.new("RGB", (100, 100), (5, 6, 7)).save(photo, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id=FID,
        photo_id="ph",
        path=str(photo),
        set_key="set-1",
        intents=["single", "digital_whatyougot"],
        mockups_dir=tmp_path / "mockups",
    )
    adapter = build_etsy_write_adapter(live=False)
    first = build_drafts(conn, USER_ID, etsy_adapter=adapter)
    assert first.drafts_built == 1 and first.pushed == 1

    listing_config.set_value(conn, USER_ID, "image_cap", 3)  # changes the hash
    report = build_drafts(conn, USER_ID, etsy_adapter=adapter)
    rebuild_listings(conn)
    assert report.skipped_already_pushed == 1
    assert report.drafts_built == 0
    assert conn.execute("SELECT COUNT(*) n FROM proj_listing_drafts").fetchone()["n"] == 1
    cfg = listing_config.get_config(conn, USER_ID)
    assert push_drafts(conn, USER_ID, cfg, adapter).pushed == 0


def test_cli_crop_preview_requires_out(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    r = runner.invoke(app, ["listings", "crop-preview", FID])
    assert r.exit_code != 0


def test_crop_preview_strips_exif_icc_comment(tmp_path, monkeypatch):
    from PIL import ImageCms

    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    c = _seed_landing(tmp_path, size=(6000, 4000))
    c.close()
    exif = Image.Exif()
    exif[0x013B] = "secret-artist"
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (6000, 4000), (9, 9, 9)).save(
        tmp_path / "p.jpg", exif=exif.tobytes(), icc_profile=icc, comment=b"SN12345"
    )
    out = tmp_path / "out"
    r = runner.invoke(app, ["listings", "crop-preview", FID, "--out", str(out)])
    assert r.exit_code == 0, r.output
    for p in out.iterdir():
        with Image.open(p) as im:
            assert "icc_profile" not in im.info
            assert "comment" not in im.info
            assert not im.getexif()
        assert b"SN12345" not in p.read_bytes()


def _seed_photo(conn, tmp_path, fid, name):
    from .helpers import seed_landing_file_with_mockup_set

    photo = tmp_path / f"{name}.jpg"
    Image.new("RGB", (100, 100), (5, 6, 7)).save(photo, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id=fid,
        photo_id=name,
        path=str(photo),
        set_key=f"set-{name}",
        intents=["single", "digital_whatyougot"],
        mockups_dir=tmp_path / "mockups",
    )


def test_unpushed_draft_then_config_change_pushes_only_newest(conn, tmp_path, monkeypatch):
    from shopsteward.pipeline.listings import drafts as drafts_mod
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.listings.models import BuildReport
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter

    _seed_photo(conn, tmp_path, FID, "ph")
    adapter = build_etsy_write_adapter(live=False)
    with monkeypatch.context() as m:  # draft A is built but its push never runs
        m.setattr(drafts_mod, "push_drafts", lambda *a, **k: BuildReport())
        assert build_drafts(conn, USER_ID, etsy_adapter=adapter).drafts_built == 1
    rebuild_listings(conn)
    draft_a = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()["draft_id"]

    listing_config.set_value(conn, USER_ID, "image_cap", 3)
    report = build_drafts(conn, USER_ID, etsy_adapter=adapter)
    rebuild_listings(conn)
    assert report.drafts_built == 1  # draft B (new config hash)
    assert report.pushed == 1
    rows = conn.execute(
        "SELECT draft_id, etsy_listing_id FROM proj_listing_drafts WHERE landing_file_id=?",
        (FID,),
    ).fetchall()
    assert len(rows) == 2
    pushed = [r for r in rows if r["etsy_listing_id"] is not None]
    assert len(pushed) == 1 and pushed[0]["draft_id"] != draft_a
    # A is still unpushed-eligible but the guard skips it on a direct push too.
    from shopsteward.pipeline.listings.push import push_drafts

    again = push_drafts(conn, USER_ID, listing_config.get_config(conn, USER_ID), adapter)
    assert again.pushed == 0 and again.skipped_already_pushed == 1


def test_push_guard_does_not_block_different_photos(conn, tmp_path):
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter

    _seed_photo(conn, tmp_path, FID, "ph1")
    _seed_photo(conn, tmp_path, "e" * 64, "ph2")
    report = build_drafts(conn, USER_ID, etsy_adapter=build_etsy_write_adapter(live=False))
    assert report.pushed == 2
    assert report.skipped_already_pushed == 0


def _append_pod_linked(conn, fid):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.created",
            payload={
                "draft_id": "pod-1",
                "landing_file_id": fid,
                "photo_id": "ph",
                "set_key": "pod",
                "provider": "gelato",
                "format": "canvas",
                "sku_source": "gelato",
                "listing_type": "physical",
                "config_hash": "x",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={"draft_id": "pod-1", "etsy_listing_id": 777},
        ),
    )


def _append_adopted(conn, fid, listing_id=123):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listing.source_adopted",
            payload={
                "draft_id": f"adopted-{listing_id}",
                "landing_file_id": fid,
                "photo_id": "ph",
                "etsy_listing_id": listing_id,
            },
        ),
    )


def test_pod_linked_row_does_not_block_digital_build_or_push(conn, tmp_path):
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.listings.push import (
        build_etsy_write_adapter,
        photo_has_digital_listing,
    )

    _seed_photo(conn, tmp_path, FID, "ph")
    _append_pod_linked(conn, FID)
    rebuild_listings(conn)
    assert photo_has_digital_listing(conn, USER_ID, FID) is None
    report = build_drafts(conn, USER_ID, etsy_adapter=build_etsy_write_adapter(live=False))
    assert report.drafts_built == 1 and report.pushed == 1
    assert report.skipped_already_pushed == 0


def test_adopted_listing_still_blocks_digital_build(conn, tmp_path):
    from shopsteward.pipeline.listings.drafts import build_drafts
    from shopsteward.pipeline.listings.push import build_etsy_write_adapter

    _seed_photo(conn, tmp_path, FID, "ph")
    _append_adopted(conn, FID)
    report = build_drafts(conn, USER_ID, etsy_adapter=build_etsy_write_adapter(live=False))
    assert report.drafts_built == 0 and report.skipped_already_pushed == 1


def test_gate3_retry_refuses_superseded_draft_when_photo_already_live(conn, tmp_path):
    from shopsteward.adapters.etsy.interface import EtsyWriteError
    from shopsteward.pipeline.listings import gate3
    from shopsteward.pipeline.listings.push import push_drafts

    from .helpers import seed_fully_built_draft

    class _FailCreate(FakeEtsyWriteAdapter):
        def create_draft_listing(self, spec):
            raise EtsyWriteError(500, "boom")

    draft_a = seed_fully_built_draft(
        conn, tmp_path, file_id=FID, photo_id="ph", title="T", set_key="s"
    )
    cfg = listing_config.get_config(conn, USER_ID)
    assert push_drafts(conn, USER_ID, cfg, _FailCreate()).push_failed == 1
    _append_adopted(conn, FID)  # the photo is now live under another draft
    adapter = FakeEtsyWriteAdapter()
    with pytest.raises(ValueError, match="already has a digital listing"):
        gate3.retry_push(conn, USER_ID, draft_a, adapter)
    assert adapter.listings == {}
