"""Slice 3a (design 2026-11-21-multi-ratio-digital-files): files_planned event,
multi-file push, plan-drift failure, gate3 retry by key, proj_listing_files."""

import io
import json

import pytest
from PIL import Image
from typer.testing import CliRunner

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings import push as push_mod
from shopsteward.pipeline.listings.digital_files import FileEncodeError
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.gate3 import retry_push
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.push import push_drafts

from .helpers import USER_ID, seed_landing_file_with_mockup_set

EXPECTED_NAMES = [
    "photo-1_full_4x6-8x12-12x18.jpg",
    "photo-1_4x5_4x5-8x10.jpg",
    "photo-1_5x7_5x7.jpg",
    "photo-1_11x14_11x14.jpg",
    "photo-1_A_A4-A3.jpg",
]


class CapturingAdapter(FakeEtsyWriteAdapter):
    def __init__(
        self,
        *,
        fail_on_file_call: int | None = None,
        fail_create: bool = False,
        fail_on_image_call: int | None = None,
        lose_response_on_file_call: int | None = None,
    ) -> None:
        super().__init__()
        self.fail_on_image_call = fail_on_image_call
        self.lose_response_on_file_call = lose_response_on_file_call
        self._image_calls = 0
        self.blobs: dict[str, bytes] = {}
        self.fail_on_file_call = fail_on_file_call
        self.fail_create = fail_create
        self._file_calls = 0

    def create_draft_listing(self, spec):
        if self.fail_create:
            raise EtsyWriteError(500, "boom")
        return super().create_draft_listing(spec)

    def upload_listing_image(self, listing_id, image, *, rank):
        self._image_calls += 1
        if self.fail_on_image_call == self._image_calls:
            raise EtsyWriteError(500, "boom")
        return super().upload_listing_image(listing_id, image, rank=rank)

    def upload_listing_file(self, listing_id, file, *, name, rank):
        self._file_calls += 1
        if self.fail_on_file_call == self._file_calls:
            raise EtsyWriteError(500, "boom")
        self.blobs[name] = file
        ref = super().upload_listing_file(listing_id, file, name=name, rank=rank)
        if self.lose_response_on_file_call == self._file_calls:
            raise EtsyWriteError(0, "transport error: ReadTimeout")  # Etsy kept the file
        return ref

    def file_uploads(self) -> list[tuple[int, str]]:
        return [(c["rank"], c["name"]) for n, c in self.calls if n == "upload_listing_file"]


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _setup(conn, tmp_path, *, ppi: int | None = 30, digital_files: bool = True):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    if ppi is not None:
        listing_config.set_value(conn, USER_ID, "digital_files.ppi", ppi)
    if not digital_files:
        listing_config.set_value(conn, USER_ID, "digital_files", None)
    path = tmp_path / "photo-1.jpg"
    Image.new("RGB", (600, 400), (10, 20, 30)).save(path, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id="f" * 64,
        photo_id="photo-1",
        path=str(path),
        set_key="set-1",
        intents=["single", "digital_whatyougot"],
        mockups_dir=tmp_path / "mockups",
    )


def _file_events(conn):
    return [e for e in read_all(conn, "listingdraft.file_attached") if e.user_id == USER_ID]


def test_build_pushes_five_ratio_files(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter()
    build_drafts(conn, USER_ID, etsy_adapter=adapter)

    assert adapter.file_uploads() == list(enumerate(EXPECTED_NAMES, start=1))
    events = _file_events(conn)
    assert [(e.payload["key"], e.payload["rank"], e.payload["name"]) for e in events] == [
        ("full", 1, EXPECTED_NAMES[0]),
        ("4x5", 2, EXPECTED_NAMES[1]),
        ("5x7", 3, EXPECTED_NAMES[2]),
        ("11x14", 4, EXPECTED_NAMES[3]),
        ("A", 5, EXPECTED_NAMES[4]),
    ]
    assert events[1].payload["sizes"] == ["4x5", "8x10"]

    planned = [e for e in read_all(conn, "listingdraft.files_planned") if e.user_id == USER_ID]
    assert len(planned) == 1
    assert planned[0].payload["focus"] == [0.5, 0.5]
    assert planned[0].payload["source_dims"] == [600, 400]
    assert planned[0].payload["digital_files_hash"]

    img = Image.open(io.BytesIO(adapter.blobs[EXPECTED_NAMES[1]]))
    assert img.size == (500, 400)
    assert round(img.info["dpi"][0]) == 30
    assert img.info["icc_profile"]
    exif = img.getexif()
    assert exif[0x013B] == "PhotosByEricD"
    assert "PhotosByEricD" in exif[0x8298]
    assert not exif.get_ifd(0x8825)  # no GPS

    rows = conn.execute(
        "SELECT key, rank, sha256, sizes_json FROM proj_listing_files ORDER BY rank"
    ).fetchall()
    assert [r["key"] for r in rows] == ["full", "4x5", "5x7", "11x14", "A"]
    assert json.loads(rows[1]["sizes_json"]) == ["4x5", "8x10"]
    plan = conn.execute("SELECT files_plan_json FROM proj_listing_drafts").fetchone()[0]
    assert json.loads(plan)["digital_files_hash"] == planned[0].payload["digital_files_hash"]


def test_retry_after_third_upload_fails_uploads_only_files_3_to_5(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(fail_on_file_call=3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    assert [e.payload["key"] for e in _file_events(conn)] == ["full", "4x5"]
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]

    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)

    assert adapter.file_uploads() == [
        (3, EXPECTED_NAMES[2]),
        (4, EXPECTED_NAMES[3]),
        (5, EXPECTED_NAMES[4]),
    ]
    assert len(_file_events(conn)) == 5
    assert len(adapter.list_listing_files(1000)) == 5


def test_plan_drift_fails_draft_and_uploads_nothing(conn, tmp_path):
    _setup(conn, tmp_path)
    build_drafts(conn, USER_ID, etsy_adapter=CapturingAdapter(fail_create=True))
    assert not _file_events(conn)

    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 31)
    rebuild_listings(conn)
    adapter = CapturingAdapter()
    report = push_drafts(conn, USER_ID, listing_config.get_config(conn, USER_ID), adapter)

    assert report.push_failed == 1
    assert adapter.calls == []
    failed = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID][-1]
    assert failed.payload["error"]["message"] == "file plan drift"
    state = conn.execute("SELECT state FROM proj_listing_drafts").fetchone()[0]
    assert state == "push_failed"


def test_digital_files_none_keeps_single_file_path(conn, tmp_path):
    _setup(conn, tmp_path, digital_files=False)
    adapter = CapturingAdapter()
    build_drafts(conn, USER_ID, etsy_adapter=adapter)

    assert adapter.file_uploads() == [(1, "photo-1.jpg")]
    assert not list(read_all(conn, "listingdraft.files_planned"))
    (event,) = _file_events(conn)
    assert "key" not in event.payload
    # legacy event folds as key "full", rank 1
    row = conn.execute("SELECT key, rank FROM proj_listing_files").fetchone()
    assert (row["key"], row["rank"]) == ("full", 1)


def test_projection_rebuild_is_idempotent(conn, tmp_path):
    _setup(conn, tmp_path)
    build_drafts(conn, USER_ID, etsy_adapter=CapturingAdapter())

    def snapshot():
        rebuild_listings(conn)
        return (
            [tuple(r) for r in conn.execute("SELECT * FROM proj_listing_files ORDER BY key")],
            [tuple(r) for r in conn.execute("SELECT * FROM proj_listing_drafts")],
        )

    first = snapshot()
    assert len(first[0]) == 5
    assert snapshot() == first


def _second_photo(conn, tmp_path):
    path = tmp_path / "photo-2.jpg"
    Image.new("RGB", (600, 400), (1, 2, 3)).save(path, "JPEG")
    seed_landing_file_with_mockup_set(
        conn,
        file_id="e" * 64,
        photo_id="photo-2",
        path=str(path),
        set_key="set-2",
        intents=["single"],
        mockups_dir=tmp_path / "mockups",
    )


def test_encode_failure_fails_draft_before_any_etsy_call_and_batch_continues(
    conn, tmp_path, monkeypatch
):
    _setup(conn, tmp_path)
    _second_photo(conn, tmp_path)
    real = push_mod.encode_file

    def flaky(path, entry, cfg):
        if "photo-1" in str(path) and entry.key == "4x5":
            raise FileEncodeError("bad icc")
        return real(path, entry, cfg)

    monkeypatch.setattr(push_mod, "encode_file", flaky)
    adapter = CapturingAdapter()
    report = build_drafts(conn, USER_ID, etsy_adapter=adapter)

    assert (report.pushed, report.push_failed) == (1, 1)
    failed = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID]
    assert [e.payload["stage"] for e in failed] == ["file"]
    assert failed[0].payload["etsy_listing_id"] is None
    # only photo-2 reached Etsy: one create, five files
    assert sum(1 for n, _ in adapter.calls if n == "create_draft_listing") == 1
    assert len(adapter.file_uploads()) == 5
    assert all(name.startswith("photo-2") for _, name in adapter.file_uploads())


def test_rollback_after_partial_push_is_drift_with_no_uploads(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(fail_on_file_call=3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]

    listing_config.set_value(conn, USER_ID, "digital_files", None)
    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)

    assert adapter.file_uploads() == []
    failed = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID][-1]
    assert failed.payload["error"]["message"].startswith("file plan drift")
    assert len(adapter.list_listing_files(1000)) == 2


def test_force_rebuild_keeps_plan_frozen_after_partial_push(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(fail_on_file_call=3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]
    plan_before = conn.execute("SELECT files_plan_json FROM proj_listing_drafts").fetchone()[0]

    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 31)
    build_drafts(conn, USER_ID, force=True, etsy_adapter=adapter)
    assert len(list(read_all(conn, "listingdraft.files_planned"))) == 1
    assert (
        conn.execute("SELECT files_plan_json FROM proj_listing_drafts").fetchone()[0] == plan_before
    )

    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)
    assert adapter.file_uploads() == []  # drift, not a mixed set
    assert len(adapter.list_listing_files(1000)) == 2


def test_projection_ignores_plan_recorded_after_files_attached(conn, tmp_path):
    from shopsteward.core.events import Event, append

    _setup(conn, tmp_path)
    build_drafts(conn, USER_ID, etsy_adapter=CapturingAdapter())
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]
    before = conn.execute("SELECT files_plan_json FROM proj_listing_drafts").fetchone()[0]
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.files_planned",
            payload={"draft_id": draft_id, "source_dims": [1, 1], "focus": [0, 0], "files": []},
        ),
    )
    rebuild_listings(conn)
    assert conn.execute("SELECT files_plan_json FROM proj_listing_drafts").fetchone()[0] == before


def test_malformed_plan_is_drift_and_no_sizes_name_has_no_suffix(conn, tmp_path):
    _setup(conn, tmp_path, ppi=None)  # 300 ppi: nothing on a 600x400 delivers a size
    cfg = listing_config.get_config(conn, USER_ID)
    path = str(tmp_path / "photo-1.jpg")
    with pytest.raises(push_mod.FilePlanDriftError):
        push_mod._checked_plan(cfg, "{}", path)
    adapter = CapturingAdapter()
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    assert adapter.file_uploads() == [(1, "photo-1_full.jpg")]


def test_crop_focus_cli_warns_when_unpushed_draft_has_plan(conn, tmp_path, monkeypatch):
    _setup(conn, tmp_path)
    build_drafts(conn, USER_ID, etsy_adapter=CapturingAdapter(fail_create=True))
    monkeypatch.setenv("SHOPSTEWARD_DB", str(tmp_path / "t.db"))
    r = CliRunner().invoke(app, ["listings", "crop-focus", "f" * 64, "--x", "0.2", "--y", "0.3"])
    assert r.exit_code == 0, r.output
    assert "listings build --force" in r.output


def test_prepare_failure_on_retry_keeps_image_stage(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(fail_on_image_call=2)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]
    failed = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID]
    assert failed[-1].payload["stage"] == "image"

    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 31)
    retry_push(conn, USER_ID, draft_id, adapter)  # drift
    failed = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID]
    assert failed[-1].payload["error"]["message"] == "file plan drift"
    assert failed[-1].payload["stage"] == "image"

    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 30)
    retry_push(conn, USER_ID, draft_id, adapter)
    imgs = [
        i["rank"]
        for e in read_all(conn, "listingdraft.images_attached")
        if e.user_id == USER_ID
        for i in e.payload["images"]
    ]
    assert sorted(imgs) == [1, 2]
    assert len(adapter.list_listing_files(1000)) == 5
    assert conn.execute("SELECT state FROM proj_listing_drafts").fetchone()[0] == "pushed"


def test_retry_reconciles_upload_that_etsy_accepted_but_timed_out(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(lose_response_on_file_call=3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]
    assert len(adapter.list_listing_files(1000)) == 3  # Etsy has 3, we recorded 2

    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)

    assert adapter.file_uploads() == [(4, EXPECTED_NAMES[3]), (5, EXPECTED_NAMES[4])]
    assert len(adapter.list_listing_files(1000)) == 5
    rec = [e for e in _file_events(conn) if e.payload.get("reconciled")]
    assert [(e.payload["key"], e.payload["rank"]) for e in rec] == [("5x7", 3)]
    assert len(_file_events(conn)) == 5


def test_retry_fails_without_uploading_if_listing_files_unreadable(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(fail_on_file_call=3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]

    def boom(listing_id):
        raise EtsyWriteError(0, "transport error: ConnectError")

    adapter.list_listing_files = boom
    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)
    assert adapter.file_uploads() == []
    assert conn.execute("SELECT state FROM proj_listing_drafts").fetchone()[0] == "push_failed"


def test_retry_fails_closed_on_unreconcilable_file(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(lose_response_on_file_call=3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]
    adapter.listings[1000]["files"][2]["name"] = "renamed.jpg"

    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)

    assert adapter.file_uploads() == []
    failed = [e for e in read_all(conn, "listingdraft.push_failed") if e.user_id == USER_ID][-1]
    assert failed.payload["error"]["message"] == "unreconciled files on listing"
    assert len(adapter.list_listing_files(1000)) == 3


def test_case_only_rename_still_reconciles(conn, tmp_path):
    _setup(conn, tmp_path)
    adapter = CapturingAdapter(lose_response_on_file_call=3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]
    adapter.listings[1000]["files"][2]["name"] = EXPECTED_NAMES[2].upper()

    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)

    assert adapter.file_uploads() == [(4, EXPECTED_NAMES[3]), (5, EXPECTED_NAMES[4])]
    assert len(adapter.list_listing_files(1000)) == 5


def test_retry_after_reset_ignores_files_attached_to_the_old_listing(conn, tmp_path):
    """Live 2026-11-22: a fake push attached all 5 keys to listing 1000; after
    a reset, the real push failed partway and the retry skipped every key."""
    from shopsteward.core.events import Event, append

    _setup(conn, tmp_path)
    adapter = CapturingAdapter()
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    draft_id = conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]
    append(conn, Event(user_id=USER_ID, type="listingdraft.reset", payload={"draft_id": draft_id}))
    rebuild_listings(conn)

    adapter.fail_on_file_call = 8  # 3rd file of the second push (5 + 3)
    build_drafts(conn, USER_ID, etsy_adapter=adapter)
    new_id = int(conn.execute("SELECT etsy_listing_id FROM proj_listing_drafts").fetchone()[0])
    assert new_id != 1000

    adapter.calls.clear()
    retry_push(conn, USER_ID, draft_id, adapter)
    assert [rank for rank, _ in adapter.file_uploads()] == [3, 4, 5]
    assert len(adapter.list_listing_files(new_id)) == 5
