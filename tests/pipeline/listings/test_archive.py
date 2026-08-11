import hashlib
import json

import pytest
from PIL import Image

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import archive, asset_store_config
from shopsteward.pipeline.listings.pod import printfile
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
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


def _point_archive_at_tmp(conn, tmp_path, *, enabled=True):
    edited = asset_store_config.load_asset_store_config().model_dump(by_alias=True)
    edited["root"] = str(tmp_path / "archive")
    edited["enabled"] = enabled
    edited_path = tmp_path / "asset_store.json"
    edited_path.write_text(json.dumps(edited))
    asset_store_config.apply(conn, USER_ID, edited_path)
    rebuild_listings(conn)


def test_archive_is_idempotent_one_row_one_file_no_dup_event(conn, tmp_path):
    # One photo produces THREE product-type drafts (acrylic/poster/canvas) --
    # the archive hook fires once per draft but must collapse to one copy.
    _point_archive_at_tmp(conn, tmp_path)
    _land(conn, tmp_path, file_id="f1", photo_id="p1")
    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())
    assert report.drafts_built == 3

    rows = conn.execute(
        "SELECT * FROM proj_asset_store WHERE user_id=? AND photo_id=?", (USER_ID, "p1")
    ).fetchall()
    assert len(rows) == 1
    events = [e for e in read_all(conn, "asset.archived") if e.user_id == USER_ID]
    assert len(events) == 1

    root = asset_store_config.resolve_root(asset_store_config.get_asset_store_config(conn, USER_ID))
    files = list((root / "p1").glob("*"))
    assert len(files) == 1


def test_archived_bytes_are_the_untouched_original(conn, tmp_path):
    _point_archive_at_tmp(conn, tmp_path)
    path = _land(conn, tmp_path, file_id="f1", photo_id="p1")
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    row = conn.execute(
        "SELECT stored_key, sha256 FROM proj_asset_store WHERE user_id=? AND photo_id=?",
        (USER_ID, "p1"),
    ).fetchone()
    root = asset_store_config.resolve_root(asset_store_config.get_asset_store_config(conn, USER_ID))
    archived_bytes = (root / row["stored_key"]).read_bytes()

    # Byte-identical to the landing original -- no resize/re-encode (verbatim
    # copyfile), unlike the sellable_file_bytes re-encode path.
    assert archived_bytes == path.read_bytes()
    assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_reprint_after_landing_and_host_cleanup_matches_archived_sha256(conn, tmp_path):
    # The money proof: build a POD draft (archives the master), delete the
    # landing file AND anything a TTL-expired host object would have held
    # (nothing persists that beyond the ephemeral hosted key), then resolve
    # the print source again -- it must come from the archive and match.
    _point_archive_at_tmp(conn, tmp_path)
    path = _land(conn, tmp_path, file_id="f1", photo_id="p1")
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    archived = conn.execute(
        "SELECT sha256 FROM proj_asset_store WHERE user_id=? AND photo_id=?", (USER_ID, "p1")
    ).fetchone()

    path.unlink()  # landing folder cleared; no hosted-object bytes are ever retained anywhere

    resolved_path = printfile.resolve_print_source_path(conn, USER_ID, "f1", "tiff_master")
    with open(resolved_path, "rb") as f:
        data = f.read()
    assert hashlib.sha256(data).hexdigest() == archived["sha256"]


def test_corrupted_archive_file_raises_on_sha256_verify(conn, tmp_path):
    _point_archive_at_tmp(conn, tmp_path)
    path = _land(conn, tmp_path, file_id="f1", photo_id="p1")
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    row = conn.execute(
        "SELECT stored_key FROM proj_asset_store WHERE user_id=? AND photo_id=?", (USER_ID, "p1")
    ).fetchone()
    root = asset_store_config.resolve_root(asset_store_config.get_asset_store_config(conn, USER_ID))
    (root / row["stored_key"]).write_bytes(b"truncated garbage")

    path.unlink()  # force the archive fallback path

    with pytest.raises(ValueError):
        printfile.resolve_print_source_path(conn, USER_ID, "f1", "tiff_master")


def test_missing_archive_file_raises_not_silently_falls_through(conn, tmp_path):
    _point_archive_at_tmp(conn, tmp_path)
    path = _land(conn, tmp_path, file_id="f1", photo_id="p1")
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    row = conn.execute(
        "SELECT stored_key FROM proj_asset_store WHERE user_id=? AND photo_id=?", (USER_ID, "p1")
    ).fetchone()
    root = asset_store_config.resolve_root(asset_store_config.get_asset_store_config(conn, USER_ID))
    (root / row["stored_key"]).unlink()

    path.unlink()

    with pytest.raises(FileNotFoundError):
        printfile.resolve_print_source_path(conn, USER_ID, "f1", "tiff_master")


def test_no_secret_or_absolute_path_in_archived_event_payload(conn, tmp_path):
    _point_archive_at_tmp(conn, tmp_path)
    _land(conn, tmp_path, file_id="f1", photo_id="p1")
    build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    events = [e for e in read_all(conn, "asset.archived") if e.user_id == USER_ID]
    assert events
    for e in events:
        assert e.user_id == USER_ID
        assert set(e.payload.keys()) == {
            "photo_id",
            "sha256",
            "bytes",
            "width",
            "height",
            "format",
            "stored_key",
            "source_landing_file_id",
        }
        stored_key = e.payload["stored_key"]
        assert not stored_key.startswith("/")
        assert ":" not in stored_key  # no Windows drive letter, no URL scheme, no credential
        assert str(tmp_path) not in stored_key


def test_disabled_config_archives_nothing_and_resolver_reports_unarchived(conn, tmp_path):
    _point_archive_at_tmp(conn, tmp_path, enabled=False)
    _land(conn, tmp_path, file_id="f1", photo_id="p1")

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    assert report.drafts_built == 3  # the build itself is unaffected
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM proj_asset_store WHERE user_id=?", (USER_ID,)
        ).fetchone()["n"]
        == 0
    )
    assert not read_all(conn, "asset.archived")


def test_archive_write_failure_degrades_and_continues_the_build(conn, tmp_path, monkeypatch):
    # Review nit #1: a disk-full/permission error while archiving must never
    # abort the operator's actual POD build -- it's an auditable side-effect
    # failure, not a build failure.
    _point_archive_at_tmp(conn, tmp_path)
    _land(conn, tmp_path, file_id="f1", photo_id="p1")

    def _raise(*args, **kwargs):
        raise OSError(28, "No space left on device")  # errno.ENOSPC

    monkeypatch.setattr(archive.shutil, "copyfile", _raise)

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    assert report.drafts_built == 3  # the build still completes
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM proj_asset_store WHERE user_id=?", (USER_ID,)
        ).fetchone()["n"]
        == 0
    )

    failed = [e for e in read_all(conn, "asset.archive_failed") if e.user_id == USER_ID]
    assert failed
    assert failed[0].payload == {
        "photo_id": "p1",
        "format": "JPEG",
        "error": "OSError: No space left on device",
    }
    for e in failed:
        assert str(tmp_path) not in json.dumps(e.payload)  # no path leaked

    rebuild_listings(conn)
    row = conn.execute(
        "SELECT etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'",
        (USER_ID,),
    ).fetchone()
    assert row["etsy_listing_id"] is None  # not linked yet, but resolve_source still works pre-link

    from shopsteward.adapters.pod.fake import FakeGelatoAdapter
    from shopsteward.pipeline.listings.pod import config as pod_config
    from shopsteward.pipeline.listings.pod.provider import link_pod_drafts

    cfg = pod_config.get_pod_config(conn, USER_ID)
    link_pod_drafts(
        conn, USER_ID, adapter=FakeGelatoAdapter(), print_file_host=FakePrintFileHost(), cfg=cfg
    )
    rebuild_listings(conn)
    linked = conn.execute(
        "SELECT etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND format='acrylic'",
        (USER_ID,),
    ).fetchone()
    ref = resolve_source(conn, USER_ID, int(linked["etsy_listing_id"]))
    assert ref is not None
    assert ref.archived is False
