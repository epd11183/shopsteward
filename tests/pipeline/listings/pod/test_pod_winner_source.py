"""Coverage: a landing winner with NO photo_id match (a manually-dropped
JPEG the ingester never paired to a known photo, decision 33) still drives
POD end to end. build_pod_drafts's photo_key falls back to the landing
file's own file_id for these rows (build.py module docstring), and
printfile.resolve_print_source_path falls back to the row's own JPEG path
when no TIFF-master sibling exists -- this test proves that fallback chain
actually produces a hosted print file, and that a too-small winner is
dropped gracefully (pod_skipped) rather than raising."""

from PIL import Image

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.pod.build import build_pod_drafts
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1


def _land_winner(conn, tmp_path, *, file_id, width, height):
    """A photo-less landing winner (photo_id=None): the manual-drop path,
    not the paired-photo path _land in test_build.py exercises."""
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
                "format": "JPEG",
                "width": width,
                "height": height,
                "color_space": "sRGB",
                "photo_id": None,
            },
        ),
    )
    rebuild_pipeline(conn)
    return path


def test_photoless_jpeg_winner_hosts_a_print_file_from_its_own_jpeg(tmp_path):
    # 2:3 landscape, well above 150dpi for every configured size (matches
    # test_build.py's _W/_H) -- no TIFF master exists, so
    # resolve_print_source_path must fall back to this row's own JPEG path.
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    _land_winner(conn, tmp_path, file_id="f-winner", width=6000, height=4000)

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    assert report.drafts_built >= 1
    assert report.print_files_hosted >= 1
    hosted = [e for e in read_all(conn, "listingdraft.print_file_hosted") if e.user_id == USER_ID]
    assert hosted


def test_too_small_photoless_winner_is_pod_skipped_not_raised(tmp_path):
    # Same 2:3 ratio, but a long edge of 900px -- 900/18in = 50dpi, below
    # min_dpi=150 for even the smallest configured variant (poster_12x18 /
    # canvas_12x18, long_edge_inches=18). Every product type must drop on
    # "dpi", and the build must complete without raising.
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    _land_winner(conn, tmp_path, file_id="f-tiny", width=900, height=600)

    report = build_pod_drafts(conn, USER_ID, print_file_host=FakePrintFileHost())

    assert report.drafts_built == 0
    assert report.pod_skipped >= 1
    skipped = [e for e in read_all(conn, "listingdraft.pod_skipped") if e.user_id == USER_ID]
    assert skipped
    assert skipped[0].payload["reason"] == "dpi"
