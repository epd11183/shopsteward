"""Slice 4 (design 2026-11-21-multi-ratio-digital-files section 8): backfill of
multi-ratio files onto live listings (B0 dry-run, B1 upload, rollback)."""

import pytest
from PIL import Image
from typer.testing import CliRunner

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.cli import app
from shopsteward.core import projections as core_projections
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.pipeline.listings import backfill as bf
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline

from .helpers import USER_ID, seed_landing_file_with_mockup_set

LID = 777
CROPS = [
    "photo-1_4x5_4x5-8x10.jpg",
    "photo-1_5x7_5x7.jpg",
    "photo-1_11x14_11x14.jpg",
    "photo-1_A_A4-A3.jpg",
]


class Flaky(FakeEtsyWriteAdapter):
    fail_on: int | None = None
    lose_on: int | None = None  # Etsy keeps the file but the call times out, re-list fails
    list_fails = False
    n = 0

    def upload_listing_file(self, listing_id, file, *, name, rank):
        self.n += 1
        if self.fail_on == self.n:
            raise EtsyWriteError(500, "boom")
        ref = super().upload_listing_file(listing_id, file, name=name, rank=rank)
        if self.lose_on == self.n:
            self.list_fails = True
            raise EtsyWriteError(0, "transport error: ReadTimeout")
        return ref

    def list_listing_files(self, listing_id):
        if self.list_fails:
            raise EtsyWriteError(0, "transport error: ConnectError")
        return super().list_listing_files(listing_id)

    def names(self, lid=LID):
        return [f.name for f in self.list_listing_files(lid)]

    def n_calls(self, kind):
        return sum(1 for k, _ in self.calls if k == kind)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _refresh(conn):
    core_projections.rebuild(conn)
    rebuild_pipeline(conn)
    rebuild_mockups(conn)
    rebuild_listings(conn)


def _world(conn, tmp_path, *, legacy=True, adapter=None):
    """One adopted live listing (777) for a 600x400 photo with a legacy rank-1 file."""
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 30)
    path = tmp_path / "photo-1.jpg"
    Image.new("RGB", (600, 400), (10, 20, 30)).save(path, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id="f" * 64,
        photo_id="photo-1",
        path=str(path),
        set_key="set-1",
        intents=["single"],
        mockups_dir=tmp_path / "mockups",
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.observed",
            payload={
                "listing_id": LID,
                "title": "Misty Ridge",
                "state": "active",
                "quantity": 999,
                "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"},
                "tags": ["wall art"],
                "description": "Prints at 8x10 and 16x20.",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listing.source_adopted",
            payload={
                "draft_id": f"adopted-{LID}",
                "etsy_listing_id": LID,
                "photo_id": "photo-1",
                "landing_file_id": "f" * 64,
            },
        ),
    )
    _refresh(conn)
    adapter = adapter or Flaky()
    adapter.seed_listing(LID)
    if legacy:
        adapter.upload_listing_file(LID, b"legacy", name="photo-1.jpg", rank=1)
    adapter.calls.clear()
    return adapter


def _run(conn, adapter, *, live=True, **kw):
    rebuild_listings(conn)  # get_config reads the projection
    cfg = listing_config.get_config(conn, USER_ID)
    return bf.backfill(conn, USER_ID, cfg, adapter, live=live, **kw)


def _types(conn, t):
    return [e for e in read_all(conn, t) if e.user_id == USER_ID]


def test_b0_makes_no_writes_and_reports_undelivered_sizes(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    report = _run(conn, adapter, live=False)

    (r,) = report.results
    assert (r.status, r.to_upload, r.undelivered, r.copy_source) == (
        "ok",
        CROPS,
        ["16x20"],
        "synced",
    )
    assert (report.would_get, report.loses_size) == (1, 1)
    assert r.existing == ["1:photo-1.jpg"]
    assert {k for k, _ in adapter.calls} == {"list_listing_files"}
    assert not _types(conn, "listingdraft.backfill_planned")


def test_b1_uploads_ranks_2_to_5_keeps_rank_1_and_rerun_is_noop(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    (r,) = _run(conn, adapter).results

    assert (r.status, r.changed) == ("uploaded", 4)
    ups = [(c["rank"], c["name"]) for k, c in adapter.calls if k == "upload_listing_file"]
    assert ups == list(enumerate(CROPS, start=2))
    assert adapter.names() == ["photo-1.jpg", *CROPS]
    done = _types(conn, "listingdraft.file_backfilled")
    assert [e.payload["key"] for e in done] == ["4x5", "5x7", "11x14", "A"]
    assert done[0].payload["sha256"] and done[0].payload["draft_id"] == f"adopted-{LID}"
    assert len(_types(conn, "listingdraft.backfill_planned")) == 1
    # never the push-path events (the projection would freeze/collide)
    assert not _types(conn, "listingdraft.file_attached")
    assert not _types(conn, "listingdraft.files_planned")

    adapter.calls.clear()
    (r2,) = _run(conn, adapter).results
    assert r2.status == "up_to_date"
    assert adapter.n_calls("upload_listing_file") == 0
    assert len(_types(conn, "listingdraft.backfill_planned")) == 1


def test_partial_failure_then_rerun_uploads_only_the_rest(conn, tmp_path):
    adapter = _world(conn, tmp_path, adapter=Flaky())
    adapter.fail_on = 4  # call 1 is the legacy upload in _world
    (r,) = _run(conn, adapter).results
    assert (r.status, r.changed) == ("failed", 2)

    adapter.fail_on = None
    adapter.calls.clear()
    (r2,) = _run(conn, adapter).results
    assert r2.status == "uploaded"
    ups = [c["name"] for k, c in adapter.calls if k == "upload_listing_file"]
    assert ups == CROPS[2:]
    assert adapter.names() == ["photo-1.jpg", *CROPS]
    assert len(_types(conn, "listingdraft.backfill_planned")) == 1


def test_drift_after_partial_run_refuses(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    adapter.fail_on = 4  # call 1 is the legacy upload in _world
    _run(conn, adapter)
    adapter.fail_on = None
    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 31)
    adapter.calls.clear()
    (r,) = _run(conn, adapter).results
    assert r.status == "refused" and "drift" in r.detail
    assert adapter.n_calls("upload_listing_file") == 0


def test_unknown_extra_file_refuses(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    adapter.upload_listing_file(LID, b"x", name="mystery.jpg", rank=2)
    adapter.calls.clear()
    (r,) = _run(conn, adapter).results
    assert r.status == "refused"
    assert adapter.n_calls("upload_listing_file") == 0


def test_missing_copyright_refuses_before_any_call(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    listing_config.set_value(conn, USER_ID, "digital_files.copyright_holder", None)
    with pytest.raises(bf.BackfillRefused):
        _run(conn, adapter)
    assert adapter.calls == []
    assert not _types(conn, "listingdraft.backfill_planned")


def test_zero_files_listing_is_never_uploaded(conn, tmp_path):
    adapter = _world(conn, tmp_path, legacy=False)
    (r,) = _run(conn, adapter).results
    assert r.status == "no_files"
    assert adapter.n_calls("upload_listing_file") == 0


def test_non_download_draft_is_skipped(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    # a POD draft linked to the same listing wins over the adopted row
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.created",
            payload={
                "draft_id": "pod-1",
                "landing_file_id": "f" * 64,
                "photo_id": "photo-1",
                "set_key": "set-1",
                "provider": "printful",
                "format": "canvas",
                "sku_source": "pod",
                "listing_type": "physical",
                "config_hash": "h",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.pushed_to_etsy",
            payload={"draft_id": "pod-1", "etsy_listing_id": LID},
        ),
    )
    rebuild_listings(conn)
    (r,) = _run(conn, adapter).results
    assert r.status == "not_digital"
    assert adapter.calls == []


def test_rollback_removes_only_backfilled_and_rerun_reuploads(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    _run(conn, adapter)
    r = bf.rollback(conn, USER_ID, adapter, LID)
    assert (r.status, r.changed) == ("removed", 4)
    assert adapter.names() == ["photo-1.jpg"]
    assert len(_types(conn, "listingdraft.file_backfill_removed")) == 4

    adapter.calls.clear()
    (again,) = _run(conn, adapter).results
    assert (again.status, again.changed) == ("uploaded", 4)
    assert adapter.names() == ["photo-1.jpg", *CROPS]


def test_rollback_refuses_when_it_would_remove_the_last_file(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    _run(conn, adapter)
    legacy = next(f for f in adapter.list_listing_files(LID) if f.name == "photo-1.jpg")
    adapter.delete_listing_file(LID, legacy.listing_file_id)  # operator removed it by hand
    adapter.calls.clear()
    r = bf.rollback(conn, USER_ID, adapter, LID)
    assert r.status == "refused"
    assert adapter.n_calls("delete_listing_file") == 0


def test_cli_refuses_when_write_gate_closed(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_WRITE", raising=False)
    result = CliRunner().invoke(app, ["listings", "backfill-files"])
    assert result.exit_code == 1
    assert "gated" in result.output


def test_timeout_where_etsy_kept_file_is_recorded_immediately(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    adapter.lose_on = 3  # call 1 is the legacy upload: this is the 2nd crop
    adapter.list_fails = False
    # the re-list inside the failure path must work for this case
    orig = adapter.list_listing_files

    def ok_list(lid):
        adapter.list_fails = False
        return orig(lid)

    adapter.list_listing_files = ok_list
    (r,) = _run(conn, adapter).results
    assert r.status == "failed"
    rec = [e for e in _types(conn, "listingdraft.file_backfilled") if e.payload.get("reconciled")]
    assert [e.payload["name"] for e in rec] == [CROPS[1]]


def test_failed_relist_is_reconciled_on_rerun_so_rollback_removes_it(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    adapter.lose_on = 3
    (r,) = _run(conn, adapter).results
    assert r.status == "failed"
    assert [e.payload["key"] for e in _types(conn, "listingdraft.file_backfilled")] == ["4x5"]

    adapter.list_fails = False
    (r2,) = _run(conn, adapter).results
    assert r2.status == "uploaded"
    done = _types(conn, "listingdraft.file_backfilled")
    assert [e.payload["key"] for e in done] == ["4x5", "5x7", "11x14", "A"]
    assert done[1].payload["reconciled"] is True
    assert bf.rollback(conn, USER_ID, adapter, LID).changed == 4
    assert adapter.names() == ["photo-1.jpg"]


def test_total_over_max_files_refuses(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    listing_config.set_value(conn, USER_ID, "digital_files.max_files", 3)
    for rank, name in ((2, "photo-1_full_4x6-8x12-12x18.jpg"), (3, CROPS[0])):
        adapter.upload_listing_file(LID, b"x", name=name, rank=rank)
    adapter.calls.clear()
    (r,) = _run(conn, adapter).results
    assert r.status == "refused" and "max_files" in r.detail
    assert adapter.n_calls("upload_listing_file") == 0


def test_name_match_is_case_insensitive(conn, tmp_path):
    adapter = _world(conn, tmp_path)
    _run(conn, adapter)
    for f in adapter.listings[LID]["files"][1:]:
        f["name"] = f["name"].upper()
    adapter.calls.clear()
    (r,) = _run(conn, adapter).results
    assert r.status == "up_to_date"
    assert adapter.n_calls("upload_listing_file") == 0


def test_rollback_cli_works_with_digital_files_null(conn, tmp_path, monkeypatch):
    adapter = _world(conn, tmp_path)
    _run(conn, adapter)
    listing_config.set_value(conn, USER_ID, "digital_files", None)
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr("shopsteward.pipeline.live_gate.live_etsy_write_open", lambda: True)
    monkeypatch.setattr(
        "shopsteward.pipeline.listings.push.build_etsy_write_adapter", lambda *, live: adapter
    )
    result = CliRunner().invoke(
        app, ["listings", "backfill-files", "--rollback", "--live", "--listing-id", str(LID)]
    )
    assert result.exit_code == 0, result.output
    assert adapter.names() == ["photo-1.jpg"]
