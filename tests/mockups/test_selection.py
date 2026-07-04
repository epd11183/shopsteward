"""Template selection: orientation filter, gallery_wall region_count gate,
hue ranking, diversity skip + relax, lex tiebreak, count > available, and
photo_avg_hue parity with templates._compute_avg_hue's method."""

import numpy as np
from PIL import Image

from shopsteward.mockups.selection import photo_avg_hue, select_templates


def _tpl(
    template_id: str, orientation: str, room_type: str, region_count: int, avg_hue: float
) -> dict:
    return {
        "template_id": template_id,
        "orientation": orientation,
        "room_type": room_type,
        "region_count": region_count,
        "avg_hue": avg_hue,
    }


def test_orientation_filter_excludes_non_matching():
    templates = [
        _tpl("a", "landscape", "living_room", 1, 30),
        _tpl("b", "portrait", "bedroom", 1, 30),
        _tpl("c", "any", "office", 1, 30),
    ]
    selected = select_templates(
        30, "landscape", templates, "single", count=3, used_room_types=set()
    )
    assert {t["template_id"] for t in selected} == {"a", "c"}


def test_gallery_wall_requires_multi_region():
    templates = [
        _tpl("single-region", "landscape", "living_room", 1, 30),
        _tpl("multi-region", "landscape", "living_room", 2, 30),
    ]
    selected = select_templates(
        30, "landscape", templates, "gallery_wall", count=2, used_room_types=set()
    )
    assert {t["template_id"] for t in selected} == {"multi-region"}


def test_hue_ranking_order_closest_first():
    templates = [
        _tpl("far", "landscape", "office", 1, 200),
        _tpl("near", "landscape", "bedroom", 1, 40),
        _tpl("exact", "landscape", "living_room", 1, 30),
    ]
    selected = select_templates(
        30, "landscape", templates, "single", count=3, used_room_types=set()
    )
    assert [t["template_id"] for t in selected] == ["exact", "near", "far"]


def test_hue_distance_is_circular():
    # 350 vs 5 -> 15 apart the short way around, closer than 40 vs 30 (10 apart)... but
    # here we check 355 (5 away from 0) ranks ahead of 40 (40 away from 0).
    templates = [
        _tpl("wraps-around", "landscape", "office", 1, 355),
        _tpl("linear-far", "landscape", "bedroom", 1, 40),
    ]
    selected = select_templates(0, "landscape", templates, "single", count=2, used_room_types=set())
    assert [t["template_id"] for t in selected] == ["wraps-around", "linear-far"]


def test_diversity_skip_and_relax():
    templates = [
        _tpl("living-1", "landscape", "living_room", 1, 30),
        _tpl("living-2", "landscape", "living_room", 1, 32),
        _tpl("bedroom-1", "landscape", "bedroom", 1, 35),
    ]
    selected = select_templates(
        30, "landscape", templates, "single", count=2, used_room_types=set()
    )
    assert [t["template_id"] for t in selected] == ["living-1", "bedroom-1"]  # skips living-2

    # relax: only living_room templates available -> repeats allowed once exhausted
    templates_only_living = [
        _tpl("living-1", "landscape", "living_room", 1, 30),
        _tpl("living-2", "landscape", "living_room", 1, 32),
    ]
    selected2 = select_templates(
        30, "landscape", templates_only_living, "single", count=2, used_room_types=set()
    )
    assert [t["template_id"] for t in selected2] == ["living-1", "living-2"]


def test_preseeded_used_room_types_are_skipped():
    templates = [
        _tpl("living-1", "landscape", "living_room", 1, 30),
        _tpl("bedroom-1", "landscape", "bedroom", 1, 32),
    ]
    selected = select_templates(
        30, "landscape", templates, "single", count=1, used_room_types={"living_room"}
    )
    assert selected[0]["template_id"] == "bedroom-1"


def test_lex_tiebreak_on_equal_hue_distance():
    templates = [
        _tpl("zzz", "landscape", "office", 1, 30),
        _tpl("aaa", "landscape", "bedroom", 1, 30),
    ]
    selected = select_templates(
        30, "landscape", templates, "single", count=1, used_room_types=set()
    )
    assert selected[0]["template_id"] == "aaa"


def test_count_greater_than_available_returns_all_available():
    templates = [_tpl("only", "landscape", "living_room", 1, 30)]
    selected = select_templates(
        30, "landscape", templates, "single", count=5, used_room_types=set()
    )
    assert len(selected) == 1
    assert selected[0]["template_id"] == "only"


def test_zero_eligible_templates_returns_empty():
    templates = [_tpl("wrong-orientation", "portrait", "living_room", 1, 30)]
    selected = select_templates(
        30, "landscape", templates, "single", count=2, used_room_types=set()
    )
    assert selected == []


def test_photo_avg_hue_matches_pure_red():
    img = Image.new("RGB", (100, 100), (255, 0, 0))
    arr = np.asarray(img)
    hue = photo_avg_hue(arr)
    assert hue < 15 or hue > 345  # near 0/360 for pure red


def test_photo_avg_hue_matches_pure_green():
    img = Image.new("RGB", (100, 100), (0, 255, 0))
    arr = np.asarray(img)
    hue = photo_avg_hue(arr)
    assert 105 <= hue <= 135  # green sits near 120 degrees
