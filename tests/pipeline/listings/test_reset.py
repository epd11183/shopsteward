"""Winners-batch reset (design: winners-batch-reset)."""

import hashlib
import io

import pytest
from PIL import Image
from typer.testing import CliRunner

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.reset import ResetIncomplete, apply_reset, plan_reset

from .helpers import USER_ID, seed_fully_built_draft, seed_landing_file_with_mockup_set

runner = CliRunner()


# plan_reset scopes candidates by re-hashing the files actually present in
# the named folder, but the fixture helpers (helpers.py) accept an arbitrary
# `file_id` and write whatever bytes they like to disk -- so every test that
# exercises plan_reset's folder scoping must pass the REAL sha256 of the
# bytes the helper is about to write, not a placeholder like "b"*64. Both
# helpers draw a fixed-color image, so the hash is a deterministic constant
# per helper.
def _jpeg_sha256(color: tuple[int, int, int]) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color).save(buf, "JPEG")
    return hashlib.sha256(buf.getvalue()).hexdigest()


_FULLY_BUILT_FILE_ID = _jpeg_sha256((1, 2, 3))  # seed_fully_built_draft's fixed photo color


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _seed_winner(conn, folder, tmp_path, *, photo_id: str, set_key: str) -> str:
    """Seeds a landing file + completed mockup set at the REAL sha256 of the
    fixed-color image seed_landing_file_with_mockup_set's own caller writes
    to disk here, so plan_reset's folder-rehash scoping finds it. 3200px:
    a landing.file_reset's follow-up scan_landing() re-validates the actual
    file on disk (min_long_edge_px=3000, config/defaults/tuning_profile.json)
    -- a small placeholder image would come back `invalid` on re-observe."""
    photo_path = folder / f"{photo_id}.jpg"
    Image.new("RGB", (3200, 3200), (5, 6, 7)).save(photo_path, "JPEG")
    file_id = hashlib.sha256(photo_path.read_bytes()).hexdigest()
    seed_landing_file_with_mockup_set(
        conn,
        file_id=file_id,
        photo_id=photo_id,
        path=str(photo_path),
        set_key=set_key,
        intents=["single"],
        mockups_dir=tmp_path / "mockups",
    )
    return file_id


def _seed_fully_built_in_folder(conn, folder, *, photo_id: str, title: str, set_key: str) -> str:
    """seed_fully_built_draft writes a fixed-color JPEG at
    folder/f"{photo_id}.jpg" -- pass its REAL sha256 as file_id so plan_reset
    (which re-hashes files actually present in `folder`) finds the row."""
    return seed_fully_built_draft(
        conn,
        folder,
        file_id=_FULLY_BUILT_FILE_ID,
        photo_id=photo_id,
        title=title,
        set_key=set_key,
    )


def test_reset_reenables_scan_and_push(conn, tmp_path):
    folder = tmp_path / "winners"
    folder.mkdir()
    _seed_winner(conn, folder, tmp_path, photo_id="photo-1", set_key="set-1")

    first = build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
    assert first.pushed == 1

    second = build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
    assert second.skipped_idempotent == 1
    assert second.pushed == 0

    plan = plan_reset(conn, USER_ID, folder, include_pushed=True)
    assert len(plan) == 1
    assert plan[0].verdict == "reset"
    report = apply_reset(
        conn, USER_ID, plan, folder=folder, reason="fake_dry_run_reset", keep_landing=False
    )
    assert report.drafts_reset == 1
    assert report.landing_files_reset == 1
    assert report.landing is not None

    third = build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
    assert third.drafts_built == 1
    assert third.pushed == 1


def test_reset_never_touches_published_draft(conn, tmp_path):
    folder = tmp_path / "winners"
    folder.mkdir()
    draft_id = _seed_fully_built_in_folder(
        conn, folder, photo_id="photo-b", title="Published", set_key="set-b"
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.pushed_to_etsy",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": "already-live",
                "listing_type": "download",
                "quantity": 999,
                "state": "draft",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="gate3.published",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": "already-live",
                "state": "active",
                "published_at": "2026-07-14T00:00:00Z",
            },
        ),
    )
    rebuild_listings(conn)

    plan = plan_reset(conn, USER_ID, folder, include_pushed=True)
    row = next(r for r in plan if r.draft_id == draft_id)
    assert row.verdict == "refused_published"

    forced = row.model_copy(update={"verdict": "reset"})
    report = apply_reset(conn, USER_ID, [forced], folder=folder, reason="x", keep_landing=True)
    assert report.drafts_reset == 0
    assert read_all(conn, "listingdraft.reset") == []

    rebuild_listings(conn)
    row_after = conn.execute(
        "SELECT state, etsy_listing_id FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row_after["state"] == "published"
    assert row_after["etsy_listing_id"] == "already-live"


def test_reset_never_touches_adopted_row(conn, tmp_path):
    folder = tmp_path / "winners"
    folder.mkdir()
    archived_path = folder / "archived.jpg"
    Image.new("RGB", (100, 100), (9, 9, 9)).save(archived_path, "JPEG")
    file_id = hashlib.sha256(archived_path.read_bytes()).hexdigest()
    draft_id = "adopted-999"
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listing.source_adopted",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": 999,
                "photo_id": "photo-x",
                "landing_file_id": file_id,
                "match_distance": None,
                "match_source": "phash",
            },
        ),
    )
    rebuild_listings(conn)

    plan = plan_reset(conn, USER_ID, folder, include_pushed=True)
    row = next(r for r in plan if r.draft_id == draft_id)
    assert row.verdict == "refused_adopted"

    forced = row.model_copy(update={"verdict": "reset"})
    report = apply_reset(conn, USER_ID, [forced], folder=folder, reason="x", keep_landing=True)
    assert report.drafts_reset == 0
    assert read_all(conn, "listingdraft.reset") == []

    rebuild_listings(conn)
    row_after = conn.execute(
        "SELECT state FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row_after is not None
    assert row_after["state"] == "adopted"


def test_pushed_draft_requires_exact_confirmation(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    folder = tmp_path / "winners"
    folder.mkdir()

    conn = connect(db)
    migrate(conn)
    draft_id = _seed_fully_built_in_folder(
        conn, folder, photo_id="photo-c", title="Pushed", set_key="set-c"
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.pushed_to_etsy",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": 5052,
                "listing_type": "download",
                "quantity": 999,
                "state": "draft",
            },
        ),
    )
    rebuild_listings(conn)
    events_before = len(read_all(conn))
    conn.close()

    result = runner.invoke(app, ["listings", "reset", str(folder), "--apply", "--include-pushed"])
    assert result.exit_code == 1, result.output
    assert "missing=" in result.output

    conn = connect(db)
    assert len(read_all(conn)) == events_before
    conn.close()

    result = runner.invoke(
        app,
        [
            "listings",
            "reset",
            str(folder),
            "--apply",
            "--include-pushed",
            "--confirm-listing-id",
            "9999",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "extra=" in result.output or "missing=" in result.output

    conn = connect(db)
    assert len(read_all(conn)) == events_before
    conn.close()

    # --keep-landing: this test exercises confirm-id matching, not the
    # landing re-observe -- the fixed 100x100 photo the helper writes is
    # below min_long_edge_px, which would otherwise correctly trip the F1
    # re-observe-succeeded check in apply_reset.
    result = runner.invoke(
        app,
        [
            "listings",
            "reset",
            str(folder),
            "--apply",
            "--include-pushed",
            "--confirm-listing-id",
            "5052",
            "--keep-landing",
        ],
    )
    assert result.exit_code == 0, result.output

    conn = connect(db)
    rebuild_listings(conn)
    row = conn.execute(
        "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND draft_id=?", (USER_ID, draft_id)
    ).fetchone()
    assert row is None
    conn.close()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    folder = tmp_path / "winners"
    folder.mkdir()

    conn = connect(db)
    migrate(conn)
    _seed_fully_built_in_folder(conn, folder, photo_id="photo-d", title="Free", set_key="set-d")
    events_before = len(read_all(conn))
    conn.close()

    result = runner.invoke(app, ["listings", "reset", str(folder)])
    assert result.exit_code == 0, result.output
    assert "Dry-run: nothing written" in result.output

    conn = connect(db)
    assert len(read_all(conn)) == events_before
    conn.close()


def test_reset_preserves_mockup_set_and_draft_id(conn, tmp_path):
    folder = tmp_path / "winners"
    folder.mkdir()
    _seed_winner(conn, folder, tmp_path, photo_id="photo-e", set_key="set-e")
    build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())

    before_draft_ids = {
        row["draft_id"]
        for row in conn.execute(
            "SELECT draft_id FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchall()
    }
    mockup_rows_before = conn.execute(
        "SELECT COUNT(*) AS n FROM proj_mockup_sets WHERE user_id=?", (USER_ID,)
    ).fetchone()["n"]

    plan = plan_reset(conn, USER_ID, folder, include_pushed=True)
    apply_reset(conn, USER_ID, plan, folder=folder, reason="x", keep_landing=False)
    build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())

    after_draft_ids = {
        row["draft_id"]
        for row in conn.execute(
            "SELECT draft_id FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchall()
    }
    mockup_rows_after = conn.execute(
        "SELECT COUNT(*) AS n FROM proj_mockup_sets WHERE user_id=?", (USER_ID,)
    ).fetchone()["n"]

    assert after_draft_ids == before_draft_ids
    assert mockup_rows_after == mockup_rows_before


def test_pod_state_cleared_by_single_delete(conn, tmp_path):
    folder = tmp_path / "winners"
    folder.mkdir()
    draft_id = _seed_fully_built_in_folder(
        conn, folder, photo_id="photo-f", title="POD", set_key="set-f"
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_created",
            payload={"draft_id": draft_id, "provider_product_id": "prov-123"},
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={"draft_id": draft_id, "etsy_listing_id": 7777},
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.print_file_hosted",
            payload={"draft_id": draft_id, "file_key": "key-1", "sha256": "s" * 64},
        ),
    )
    rebuild_listings(conn)

    row = conn.execute(
        "SELECT provider_product_id, pod_status, print_file_key FROM proj_listing_drafts "
        "WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row["provider_product_id"] == "prov-123"
    assert row["pod_status"] == "linked"
    assert row["print_file_key"] == "key-1"

    plan = plan_reset(conn, USER_ID, folder, include_pushed=True)
    report = apply_reset(conn, USER_ID, plan, folder=folder, reason="x", keep_landing=True)
    assert report.drafts_reset == 1

    rebuild_listings(conn)
    row_after = conn.execute(
        "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND draft_id=?", (USER_ID, draft_id)
    ).fetchone()
    assert row_after is None


def test_apply_reset_raises_if_reobserve_comes_back_invalid(conn, tmp_path):
    """F1 guard: the fully-built-draft fixture's photo is 100x100, below
    min_long_edge_px (3000) -- a real re-observe after reset must come back
    landing.file_invalid, and apply_reset must refuse to report success."""
    folder = tmp_path / "winners"
    folder.mkdir()
    draft_id = _seed_fully_built_in_folder(
        conn, folder, photo_id="photo-h", title="TooSmall", set_key="set-h"
    )
    tuning.seed(conn, USER_ID, TUNING_PROFILE_PATH)

    plan = plan_reset(conn, USER_ID, folder, include_pushed=True)
    with pytest.raises(ResetIncomplete):
        apply_reset(conn, USER_ID, plan, folder=folder, reason="x", keep_landing=False)

    # The draft row is gone (that half of the reset did commit) -- the
    # error is there so an operator doesn't walk away thinking the file is
    # usable again when it's actually now landing-invalid.
    rebuild_listings(conn)
    row = conn.execute(
        "SELECT 1 FROM proj_listing_drafts WHERE user_id=? AND draft_id=?", (USER_ID, draft_id)
    ).fetchone()
    assert row is None


def test_scoped_to_folder(conn, tmp_path):
    winners = tmp_path / "winners"
    winners.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    draft_id = _seed_fully_built_in_folder(
        conn, elsewhere, photo_id="photo-g", title="Elsewhere", set_key="set-g"
    )

    plan = plan_reset(conn, USER_ID, winners, include_pushed=True)
    assert all(r.draft_id != draft_id for r in plan)


def test_apply_reset_refuses_a_row_that_gained_an_external_id_after_plan(conn, tmp_path):
    """F3 guard: a plan-time "reset" verdict (freely resettable, no external
    id yet) must not still authorize a reset if a concurrent push gave the
    row a real etsy_listing_id before apply_reset ran -- that id was never
    shown to the operator for confirmation."""
    folder = tmp_path / "winners"
    folder.mkdir()
    draft_id = _seed_fully_built_in_folder(
        conn, folder, photo_id="photo-i", title="Free", set_key="set-i"
    )

    plan = plan_reset(conn, USER_ID, folder, include_pushed=True)
    row = next(r for r in plan if r.draft_id == draft_id)
    assert row.verdict == "reset"
    assert row.etsy_listing_id is None

    # Simulate a concurrent `shop build`/push landing an id after the plan
    # was computed but before apply_reset runs.
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.pushed_to_etsy",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": 4242,
                "listing_type": "download",
                "quantity": 999,
                "state": "draft",
            },
        ),
    )
    rebuild_listings(conn)

    report = apply_reset(conn, USER_ID, [row], folder=folder, reason="x", keep_landing=True)
    assert report.drafts_reset == 0
    assert read_all(conn, "listingdraft.reset") == []

    rebuild_listings(conn)
    row_after = conn.execute(
        "SELECT etsy_listing_id, state FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, draft_id),
    ).fetchone()
    assert row_after["etsy_listing_id"] == "4242"
    assert row_after["state"] == "pushed"
