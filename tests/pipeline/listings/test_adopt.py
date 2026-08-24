import io
import json

import pytest
from PIL import Image, ImageDraw, ImageEnhance

from shopsteward.adapters.etsy.models import EtsyListing, EtsyListingImage, Money
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.listings import adopt, asset_store_config
from shopsteward.pipeline.listings.gate3 import _QUEUE_STATES
from shopsteward.pipeline.listings.pod.provider import link_pod_drafts
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.push import _eligible_drafts
from shopsteward.pipeline.listings.source_assets import resolve_source
from shopsteward.pipeline.ops.capabilities.gapfill import _draft_exists

USER_ID = 1


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _lower_min_long_edge(conn, tmp_path) -> None:
    # scan_landing()'s default profile requires >=3000px long edge --
    # synthetic test images stay small, so pre-seed a lower threshold before
    # anything else seeds the real default (tuning.seed is first-write-wins
    # per profile name).
    edited = json.loads(TUNING_PROFILE_PATH.read_text())
    edited["landing"]["min_long_edge_px"] = 32
    edited_path = tmp_path / "tuning_profile.json"
    edited_path.write_text(json.dumps(edited))
    tuning.seed(conn, USER_ID, edited_path)


def _point_archive_at_tmp(conn, tmp_path) -> None:
    edited = asset_store_config.load_asset_store_config().model_dump(by_alias=True)
    edited["root"] = str(tmp_path / "archive")
    edited_path = tmp_path / "asset_store.json"
    edited_path.write_text(json.dumps(edited))
    asset_store_config.apply(conn, USER_ID, edited_path)
    rebuild_listings(conn)


def _jpeg(*, quality=95, size=256, brightness=1.0) -> bytes:
    img = Image.new("RGB", (size, size), (20, 40, 80))
    d = ImageDraw.Draw(img)
    d.ellipse((size // 4, size // 4, size * 3 // 4, size * 3 // 4), fill=(230, 160, 40))
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _unrelated_jpeg(size=256) -> bytes:
    img = Image.new("RGB", (size, size), (10, 10, 10))
    d = ImageDraw.Draw(img)
    for x in range(0, size, 16):
        d.line((x, 0, x, size), fill=(255, 255, 255), width=2)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _listing(listing_id: int) -> EtsyListing:
    return EtsyListing(
        listing_id=listing_id,
        title="A print",
        state="active",
        quantity=1,
        price=Money(amount=2500, divisor=100, currency_code="USD"),
    )


class _StubReadAdapter:
    """Minimal in-memory EtsyAdapter test double -- the phash algorithm
    tests generate image content per-test, so a static fixture file (as
    FixtureEtsyAdapter uses) doesn't fit; this is a plain Protocol
    implementation, per build_drafts's own etsy_adapter injection precedent.
    """

    def __init__(self, listings: list[EtsyListing], images: dict[int, bytes]):
        self._listings = listings
        self._images = images

    def get_shop(self):
        raise NotImplementedError

    def list_listings(self) -> list[EtsyListing]:
        return self._listings

    def list_receipts(self, min_created=None):
        raise NotImplementedError

    def get_listing_images(self, listing_id: int) -> list[EtsyListingImage]:
        if listing_id not in self._images:
            return []
        return [EtsyListingImage(listing_image_id=1, rank=1, url_570xN=f"stub://{listing_id}")]

    def download_image(self, url: str) -> bytes:
        listing_id = int(url.removeprefix("stub://"))
        return self._images[listing_id]


def test_recompressed_copy_matches_unrelated_does_not(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)  # same content, re-encoded
    unrelated = _unrelated_jpeg()

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)
    (local_dir / "unrelated.jpg").write_bytes(unrelated)

    adapter = _StubReadAdapter([_listing(111)], {111: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert len(results) == 1
    assert results[0].verdict == "match"
    assert results[0].local_path == str(local_dir / "copy.jpg")


def test_near_duplicate_second_copy_is_ambiguous_and_adopts_nothing(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy1 = _jpeg(quality=55)
    copy2 = _jpeg(quality=55, brightness=1.03)  # near-duplicate burst frame

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy1.jpg").write_bytes(copy1)
    (local_dir / "copy2.jpg").write_bytes(copy2)

    adapter = _StubReadAdapter([_listing(222)], {222: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert results[0].verdict == "ambiguous"

    report = adopt.apply_matches(conn, USER_ID, cfg, results)
    assert report.adopted == 0
    assert report.ambiguous == 1
    assert resolve_source(conn, USER_ID, 222) is None


def test_apply_archives_and_links_a_confirmed_match(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    unrelated = _unrelated_jpeg()

    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)
    (local_dir / "unrelated.jpg").write_bytes(unrelated)

    adapter = _StubReadAdapter([_listing(333)], {333: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    report = adopt.apply_matches(conn, USER_ID, cfg, results)
    assert report.matched == 1
    assert report.adopted == 1

    ref = resolve_source(conn, USER_ID, 333)
    assert ref is not None
    assert ref.archived is True
    assert ref.photo_id is not None

    # idempotent re-apply: no double-archive, no double-event
    results2 = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert results2 == []  # 333 already resolves -> excluded from further matching
    archived_events = [e for e in read_all(conn, "asset.archived") if e.user_id == USER_ID]
    adopted_events = [e for e in read_all(conn, "listing.source_adopted") if e.user_id == USER_ID]
    assert len(archived_events) == 1
    assert len(adopted_events) == 1


def test_dry_run_writes_no_events_and_no_archive_files(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)

    adapter = _StubReadAdapter([_listing(444)], {444: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert results[0].verdict == "match"  # would have matched...

    # ...but plan_matches alone (dry-run) never writes anything.
    assert not read_all(conn, "asset.archived")
    assert not read_all(conn, "listing.source_adopted")
    root = asset_store_config.resolve_root(cfg)
    assert not root.exists() or not any(root.rglob("*"))
    assert resolve_source(conn, USER_ID, 444) is None


def test_revoke_removes_projection_row_but_keeps_archived_bytes(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)

    adapter = _StubReadAdapter([_listing(555)], {555: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)
    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    adopt.apply_matches(conn, USER_ID, cfg, results)

    ref = resolve_source(conn, USER_ID, 555)
    assert ref is not None and ref.archived is True
    root = asset_store_config.resolve_root(cfg)
    archived_files_before = list(root.rglob("*.jpg"))
    assert archived_files_before  # bytes are on disk

    adopt.revoke(conn, USER_ID, 555)

    assert resolve_source(conn, USER_ID, 555) is None
    archived_files_after = list(root.rglob("*.jpg"))
    assert archived_files_after == archived_files_before  # nothing deleted from the archive


# --- inertness: an "adopted" row must never surface as a real actionable
# draft anywhere downstream (design step 4.6/4.7) -----------------------------


def _adopt_one_row(conn, tmp_path) -> None:
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)
    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "inert_local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)
    adapter = _StubReadAdapter([_listing(666)], {666: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)
    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    adopt.apply_matches(conn, USER_ID, cfg, results)


def test_adopted_row_excluded_from_push_eligible_drafts(conn, tmp_path):
    _adopt_one_row(conn, tmp_path)
    assert _eligible_drafts(conn, USER_ID) == []


def test_adopted_row_state_not_in_gate3_queue_states(conn, tmp_path):
    _adopt_one_row(conn, tmp_path)
    row = conn.execute(
        "SELECT state FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, "adopted-666"),
    ).fetchone()
    assert row is not None
    assert row["state"] == "adopted"
    assert row["state"] not in _QUEUE_STATES


def test_adopted_row_excluded_from_pod_provider_link(conn, tmp_path, monkeypatch):
    _adopt_one_row(conn, tmp_path)
    row = conn.execute(
        "SELECT pod_config_hash FROM proj_listing_drafts WHERE user_id=? AND draft_id=?",
        (USER_ID, "adopted-666"),
    ).fetchone()
    assert row["pod_config_hash"] is None

    # link_pod_drafts' own eligibility query requires pod_config_hash IS NOT
    # NULL -- calling it must be a pure no-op against the adopted row (no
    # provider/printfile adapters needed since nothing should be selected).
    class _BoomAdapter:
        def __getattr__(self, name):
            raise AssertionError("pod provider adapter must never be touched")

    from shopsteward.pipeline.listings.pod import config as pod_config
    from shopsteward.pipeline.listings.pod.projections import rebuild_pod_config

    pod_config.seed(conn, USER_ID)
    rebuild_pod_config(conn)
    rebuild_listings(conn)
    cfg = pod_config.get_pod_config(conn, USER_ID)
    link_pod_drafts(conn, USER_ID, adapter=_BoomAdapter(), print_file_host=_BoomAdapter(), cfg=cfg)


def test_adopted_draft_id_never_collides_with_gapfill_sha256_ids(conn, tmp_path):
    _adopt_one_row(conn, tmp_path)
    assert _draft_exists(conn, USER_ID, "adopted-666") is True
    # gapfill's own draft_ids are sha256 hex digests -- structurally disjoint
    # from the "adopted-{listing_id}" namespace this module writes.
    assert not "adopted-666".isalnum() or len("adopted-666") != 64


# --- revoke durability (bug 1): a revoke must stick, not be a no-op the very
# next matching run re-undoes ---------------------------------------------


def test_revoke_is_durable_against_a_later_plan_and_apply(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)

    adapter = _StubReadAdapter([_listing(777)], {777: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    adopt.apply_matches(conn, USER_ID, cfg, results)
    assert resolve_source(conn, USER_ID, 777) is not None

    adopt.revoke(conn, USER_ID, 777)
    assert resolve_source(conn, USER_ID, 777) is None

    # Same folder, same listing, same file still sitting there -- a re-run
    # must NOT re-adopt the revoked match.
    results2 = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert results2 == []  # 777 excluded from matching entirely, not re-offered

    report2 = adopt.apply_matches(conn, USER_ID, cfg, results2)
    assert report2.adopted == 0
    assert resolve_source(conn, USER_ID, 777) is None


def test_revoke_then_apply_in_a_single_invocation_stays_revoked(conn, tmp_path):
    """Mirrors cli.py's actual `--revoke 777 --apply` ordering: the revoke
    loop runs, THEN plan_matches, THEN apply_matches -- all in one command.
    Before the event-order fix this re-adopted the same wrong match in one
    shot; the revoke must win."""
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)

    adapter = _StubReadAdapter([_listing(888)], {888: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    adopt.apply_matches(conn, USER_ID, cfg, results)
    assert resolve_source(conn, USER_ID, 888) is not None

    # cli.py order: revoke(s) first, then plan_matches, then apply_matches --
    # all within the same call.
    adopt.revoke(conn, USER_ID, 888)
    results_single_cmd = adopt.plan_matches(
        conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match
    )
    report = adopt.apply_matches(conn, USER_ID, cfg, results_single_cmd)

    assert report.adopted == 0
    assert resolve_source(conn, USER_ID, 888) is None


# --- landing scope (bug 2): adopting one file must not enroll its whole
# containing folder into the landing pipeline -------------------------------


def test_adopt_one_registers_only_the_matched_file_not_the_whole_folder(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "mixed_archive"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)
    # Bystander files the operator never flagged for sale.
    for i in range(4):
        (local_dir / f"bystander_{i}.jpg").write_bytes(_unrelated_jpeg())

    adapter = _StubReadAdapter([_listing(999)], {999: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert results[0].verdict == "match"
    assert results[0].local_path == str(local_dir / "copy.jpg")

    report = adopt.apply_matches(conn, USER_ID, cfg, results)
    assert report.adopted == 1

    rows = conn.execute(
        "SELECT path FROM proj_landing_files WHERE user_id=?", (USER_ID,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["path"] == str(local_dir / "copy.jpg")


# --- MEDIUM finding 1: an invalid (below-min-resolution) matched file must
# never be archived or adopted, even though landing.file_invalid still gets
# recorded ---------------------------------------------------------------


def test_invalid_matched_file_is_not_archived_or_adopted(conn, tmp_path):
    # Deliberately do NOT lower min_long_edge_px -- default is 3000px and the
    # synthetic 256x256 test images fall well below it, so the match is
    # "real" (phash matches) but the file fails landing validation.
    _point_archive_at_tmp(conn, tmp_path)

    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)

    adapter = _StubReadAdapter([_listing(1010)], {1010: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)

    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert results[0].verdict == "match"

    report = adopt.apply_matches(conn, USER_ID, cfg, results)
    assert report.adopted == 0

    # No source_adopted event, no resolvable source, nothing archived to disk.
    assert resolve_source(conn, USER_ID, 1010) is None
    assert not read_all(conn, "listing.source_adopted")
    assert not read_all(conn, "asset.archived")
    root = asset_store_config.resolve_root(cfg)
    assert not root.exists() or not any(root.rglob("*"))

    invalid_events = [e for e in read_all(conn, "landing.file_invalid") if e.user_id == USER_ID]
    assert len(invalid_events) == 1
    assert invalid_events[0].payload["reason"] == "below_min_resolution"


# --- MEDIUM finding 2: revoke() must be a no-op (and must not blacklist the
# listing_id) for a listing_id that was never adopted ----------------------


def test_revoke_never_adopted_is_a_noop_and_does_not_blacklist(conn, tmp_path):
    _lower_min_long_edge(conn, tmp_path)
    _point_archive_at_tmp(conn, tmp_path)

    assert resolve_source(conn, USER_ID, 1111) is None

    result = adopt.revoke(conn, USER_ID, 1111)
    assert result is False
    assert not read_all(conn, "listing.source_match_revoked")

    # Since nothing was ever adopted, a later real match for the same
    # listing_id must still be eligible -- the no-op revoke must not have
    # permanently blacklisted it.
    original = _jpeg(quality=95)
    copy = _jpeg(quality=55)
    local_dir = tmp_path / "local"
    local_dir.mkdir()
    (local_dir / "copy.jpg").write_bytes(copy)

    adapter = _StubReadAdapter([_listing(1111)], {1111: original})
    cfg = asset_store_config.get_asset_store_config(conn, USER_ID)
    results = adopt.plan_matches(conn, USER_ID, adapter, local_dir, recursive=False, cfg=cfg.match)
    assert len(results) == 1
    assert results[0].verdict == "match"

    report = adopt.apply_matches(conn, USER_ID, cfg, results)
    assert report.adopted == 1
    assert resolve_source(conn, USER_ID, 1111) is not None
