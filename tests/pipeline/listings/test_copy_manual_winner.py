"""Manual (photo-less) winners have photo_id IS NULL in proj_landing_files,
but Task 2's vision-copy pass scores them under the synthetic id
file-<file_id[:12]> in proj_scores. build_drafts must thread that synthetic
id into generate_copy so these winners' listing copy reflects the vision
verdict instead of falling back to the "no score row" generic default."""

from PIL import Image

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.projections import rebuild_pipeline

from .helpers import USER_ID

FILE_ID = "a" * 64
SYNTHETIC_PHOTO_ID = f"file-{FILE_ID[:12]}"
SET_KEY = f"set-{FILE_ID}"


def _seed_manual_winner_with_mockup_set(conn, tmp_path):
    """A photo-less/manual winner: landing.file_observed carries
    photo_id=None (proj_landing_files.photo_id IS NULL), matching how the
    Etsy landing folder ingests a winner with no upstream hero photo_id.
    The mockup events tag rows with the synthetic id purely for directory
    naming/bookkeeping -- proj_mockup_sets/proj_mockups are joined by
    landing_file_id + set_key only (mockups/projections.py), not photo_id."""
    path = tmp_path / "winner.jpg"
    Image.new("RGB", (100, 100), (5, 6, 7)).save(path, "JPEG")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": FILE_ID,
                "path": str(path),
                "base_name": None,
                "format": "JPEG",
                "width": 4000,
                "height": 3000,
                "color_space": "sRGB",
                "photo_id": None,
            },
        ),
    )
    mockup_path = tmp_path / "mockups" / "single.jpg"
    mockup_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (50, 50), (1, 2, 3)).save(mockup_path, "JPEG")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="mockup.generated",
            payload={
                "photo_id": SYNTHETIC_PHOTO_ID,
                "landing_file_id": FILE_ID,
                "set_key": SET_KEY,
                "intent": "single",
                "template_id": None,
                "path": str(mockup_path),
                "params": {},
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="mockupset.completed",
            payload={
                "photo_id": SYNTHETIC_PHOTO_ID,
                "landing_file_id": FILE_ID,
                "set_key": SET_KEY,
                "count": 1,
                "config_hash": "mockup-cfg-hash",
                "template_library_hash": "template-lib-hash",
            },
        ),
    )
    rebuild_pipeline(conn)
    rebuild_mockups(conn)


def _seed_vision_score(conn, *, subject="trail runner"):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="photo.scored",
            payload={
                "photo_id": SYNTHETIC_PHOTO_ID,
                "composite": 0.0,
                "scores": {},
                "vision": {
                    "triage": {
                        "verdict": {
                            "commercial_score": 80,
                            "subject": subject,
                            "strongest_room_style": "modern",
                            "one_risk": "busy background",
                            "rationale": "dynamic motion",
                        }
                    }
                },
            },
        ),
    )
    rebuild_pipeline(conn)


def test_manual_winner_draft_copy_reflects_vision_subject(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)

    _seed_manual_winner_with_mockup_set(conn, tmp_path)
    _seed_vision_score(conn, subject="trail runner")

    result = build_drafts(conn, USER_ID)
    assert result.drafts_built == 1

    copy_events = [e for e in read_all(conn, "listingdraft.copy_generated") if e.user_id == USER_ID]
    assert len(copy_events) == 1
    payload = copy_events[0].payload

    # FixtureCopyAdapter titles/describes off inputs.subject; a generic
    # fallback ("Wildlife"/"wildlife") means the synthetic id never reached
    # proj_scores -- i.e. the bug this test guards against.
    assert "Trail Runner" in payload["title"]
    assert "trail runner" in payload["description"]
    assert "Wildlife" not in payload["title"]
