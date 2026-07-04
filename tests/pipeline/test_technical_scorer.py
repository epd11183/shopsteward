import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from shopsteward.pipeline.models import TuningProfile
from shopsteward.pipeline.scorers import ScoreContext
from shopsteward.pipeline.scorers.technical import TechnicalScorer

DEFAULTS_PATH = Path(__file__).parents[2] / "config" / "defaults" / "tuning_profile.json"


@pytest.fixture()
def profile() -> TuningProfile:
    return TuningProfile.model_validate(json.loads(DEFAULTS_PATH.read_text()))


def _make_checkerboard(path: Path, size: int = 4096, square: int = 16) -> None:
    arr = np.zeros((size, size), dtype=np.uint8)
    for y in range(0, size, square * 2):
        for x in range(0, size, square * 2):
            arr[y : y + square, x : x + square] = 255
            arr[y + square : y + square * 2, x + square : x + square * 2] = 255
    Image.fromarray(arr).convert("RGB").save(path, "JPEG", quality=95)


def _ctx(path: Path, profile: TuningProfile) -> ScoreContext:
    return ScoreContext(
        user_id=1,
        photo_id="p1",
        base_name="p1",
        jpeg_path=str(path),
        profile=profile,
        vision=None,  # technical scorer never touches vision
    )


def test_sharp_image_scores_higher_than_blurred(tmp_path, profile):
    sharp_path = tmp_path / "sharp.jpg"
    blurred_path = tmp_path / "blurred.jpg"
    _make_checkerboard(sharp_path)

    gray = cv2.imread(str(sharp_path), cv2.IMREAD_GRAYSCALE)
    blurred = cv2.GaussianBlur(gray, (31, 31), 12)
    Image.fromarray(blurred).convert("RGB").save(blurred_path, "JPEG", quality=95)

    scorer = TechnicalScorer()
    sharp_result = scorer.score(_ctx(sharp_path, profile))
    blurred_result = scorer.score(_ctx(blurred_path, profile))

    assert sharp_result.score > blurred_result.score
    assert sharp_result.detail["laplacian_variance"] > blurred_result.detail["laplacian_variance"]


def test_near_black_image_gets_shadow_clip_penalty(tmp_path, profile):
    path = tmp_path / "black.jpg"
    arr = np.zeros((4096, 4096), dtype=np.uint8)
    Image.fromarray(arr).convert("RGB").save(path, "JPEG", quality=95)

    scorer = TechnicalScorer()
    result = scorer.score(_ctx(path, profile))

    assert result.detail["shadow_clip_pct"] > profile.scoring.technical["clip_shadow_pct_max"]
    assert result.detail["exposure_score"] < 100


def test_small_image_capped_at_forty(tmp_path, profile):
    path = tmp_path / "small.jpg"
    _make_checkerboard(path, size=100, square=4)

    scorer = TechnicalScorer()
    result = scorer.score(_ctx(path, profile))

    assert result.score <= 40
    assert result.detail["resolution_guard_applied"] is True
    assert result.detail["long_edge_px"] == 100


def test_unreadable_image_raises(tmp_path, profile):
    path = tmp_path / "bad.jpg"
    path.write_bytes(b"not an image")

    scorer = TechnicalScorer()
    with pytest.raises(ValueError):
        scorer.score(_ctx(path, profile))
