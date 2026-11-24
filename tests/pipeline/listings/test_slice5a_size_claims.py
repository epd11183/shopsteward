"""Slice 5a: pure description rewrite (synthetic text only)."""

import json
from pathlib import Path

import pytest

from shopsteward.pipeline.listings.copy import load_wording
from shopsteward.pipeline.listings.digital_files import FileEntry, FilePlan
from shopsteward.pipeline.listings.models import ListingConfig
from shopsteward.pipeline.listings.size_claims import fix_description
from shopsteward.pipeline.listings.size_guard import undelivered_sizes

_DEFAULTS = Path(__file__).parents[3] / "config" / "defaults" / "listing.json"
DCFG = ListingConfig.model_validate(json.loads(_DEFAULTS.read_text("utf-8"))).digital_files
assert DCFG is not None


def _plan(full: list[str] | None = None) -> FilePlan:
    box = (0, 0, 10, 10)
    return FilePlan(
        source_dims=(600, 400),
        focus=(0.5, 0.5),
        files=[
            FileEntry(key="full", rank=1, crop_box=box, sizes=full or []),
            FileEntry(key="4x5", rank=2, crop_box=box, sizes=["4x5", "8x10"]),
            FileEntry(key="5x7", rank=3, crop_box=box, sizes=["5x7"]),
            FileEntry(key="A", rank=4, crop_box=box, sizes=["A4", "A3"]),
        ],
    )


PLAN = _plan()
OLD_TAIL = "original work and is never AI-generated or AI-edited."
NEW_SENTENCE = "The photograph itself is the artist's original work: it is not AI-generated"


def fix(text: str, plan: FilePlan = PLAN) -> str | None:
    return fix_description(text, plan, DCFG)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("4x5 · 5x7 · 8x10 · 11x14 · 16x20 · A2 · A3 · A4", "4x5 · 5x7 · 8x10 · A3 · A4"),
        ("4x5 • 5x7 • 8x10 • 11x14 • 16x20 • A2 • A3 • A4", "4x5 • 5x7 • 8x10 • A3 • A4"),
        ("4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4", "4x5, 5x7, 8x10, A3, A4"),
        (
            "✔ Sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, and A4 included",
            "✔ Sizes: 4x5, 5x7, 8x10, A3, and A4 included",
        ),
        (
            "✔ Sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3 or A4 too",
            "✔ Sizes: 4x5, 5x7, 8x10, A3 or A4 too",
        ),
        (
            "✔ 8 print sizes in total (4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4)",
            "✔ 5 print sizes in total (4x5, 5x7, 8x10, A3, A4)",
        ),
        (
            "4x5 in, 5x7 in, 8x10 in, 11x14 in, 16x20 in, A4, A3, A2",
            "4x5 in, 5x7 in, 8x10 in, A4, A3",
        ),
        (
            "4x5, 5x7, 8x10, 11x14, 16x20, A4, A3, A2 - pick the size you like.",
            "4x5, 5x7, 8x10, A4, A3 - pick the size you like.",
        ),
        ("8x10, 16x20 and A2", "8x10"),
        ("8x10, 16x20, and A3", "8x10 and A3"),
    ],
)
def test_run_shapes(line, expected):
    assert fix(f"Intro line.\n{line}\nOutro line.") == f"Intro line.\n{expected}\nOutro line."


@pytest.mark.parametrize(
    "line",
    [
        "6x4 in, 12x8 in, 18x12 in, 24x16 in, 30x20 in, 36x24 in",
        "4x6, 8x12, 12x18, 16x24, 20x30, 24x36",
        "Sizes: 16x20, 11x14.",
        "✔ Available print sizes: 16x20 and 11x14",
        "Dimensions: 16x20, 24x36;",
        "✔ 16x20",
        "✔ A2",
        "24x36",
        "• 24x16 in",
    ],
)
def test_lines_dropped(line):
    assert fix(f"Intro line.\n{line}\nOutro line.") == "Intro line.\nOutro line."


def test_delivered_single_line_kept_and_nothing_to_change_is_none():
    assert fix("Intro.\n✔ 8x10\nOutro.") is None


def test_full_sizes_count_only_when_plan_says_so():
    text = "Intro.\n4x6, 8x10, 16x20"
    assert fix(text) == "Intro.\n8x10"
    # full delivers 4x6 and 16x20 -> nothing undelivered -> unchanged -> None
    assert fix(text, _plan(full=["4x6", "16x20"])) is None


def test_disclosure_swap_straight_and_curly_apostrophe():
    assert "The photograph itself" in load_wording()["ai_disclosure_line"]
    for ap in ("'", "’"):
        old = f"The photograph itself is the artist{ap}s {OLD_TAIL}"
        text = f"Room scenes are AI-generated staging mockups. {old}\n16x20"
        out = fix(text)
        assert out is not None
        assert f"mockups. The photograph itself is the artist{ap}s" in out
        assert "never AI-generated or AI-edited" not in out
        assert "it is not AI-generated, and AI is never used to add anything to it." in out
        assert out.endswith("it.")  # the 16x20 line is gone


def test_disclosure_only_change_is_returned():
    old = f"The photograph itself is the artist's {OLD_TAIL}"
    out = fix(f"Intro.\n{old}")
    assert out is not None and NEW_SENTENCE in out


def test_untouched_lines_and_crlf_are_byte_identical():
    text = "Intro  \r\n\r\n4x5, 11x14, A3\r\nOutro with trailing space \r\n"
    assert fix(text) == "Intro  \r\n\r\n4x5, A3\r\nOutro with trailing space \r\n"


@pytest.mark.parametrize(
    "text",
    [
        "Looks great at 16x20 in your hall.",  # prose claim: hand edit
        "Sizes 8x10 or 16x20 as you wish, and 11x14 too.",  # a claim outside any run survives
    ],
)
def test_unhandled_shape_returns_none(text):
    assert fix(text) is None


def test_none_when_result_still_claims_an_undelivered_size():
    # the rewrite cannot see the second, prose claim -> acceptance check says hand edit
    assert fix("4x5, 16x20\nAlso fits a 24x36 frame nicely.") is None


def test_acceptance_uses_injected_check_and_matches_default():
    text = "4x5, 16x20"
    assert fix_description(text, PLAN, DCFG, undelivered_sizes) == "4x5"
    # a check that never reports anything leaves the text unchanged -> None
    assert fix_description(text, PLAN, DCFG, lambda *_: []) is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "✔ 8 print sizes, one per frame (4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4)",
            "✔ 5 print sizes, one per frame (4x5, 5x7, 8x10, A3, A4)",
        ),
        ("Sizes: 11x14, 16x20", None),  # dropped with its label (see test below)
    ],
)
def test_count_rule_only_touches_the_size_count(line, expected):
    out = fix(f"Intro.\n{line}\nOutro.")
    assert out == (f"Intro.\n{expected}\nOutro." if expected else "Intro.\nOutro.")


@pytest.mark.parametrize(
    "line",
    [
        "Hang it big: 11x14 or 16x20 looks amazing above a sofa.",
        "Perfect gift. 16x20 and 11x14 fit standard frames.",
        "Printable in 5 sizes: 8x10, 11x14, 16x20",  # count differs from the run
        "2 sizes, 3 sizes: 8x10, 16x20",  # two count matches
    ],
)
def test_prose_or_inconsistent_count_is_a_hand_edit(line):
    assert fix(f"Intro.\n{line}") is None


@pytest.mark.parametrize(
    "line",
    [
        "Hang it big: 11x14 or 16x20 - looks amazing above a sofa.",
        "Gift idea: 16x20, 11x14 (not suitable for small frames)",
        "Large prints: 16x20, 24x36 - the colors really pop at this size!",
        "✔ 16x20, 11x14 — museum quality, no watermark",
        "Note: 11x14, 16x20 - frame not included",
        "Bigger: 16x20, 11x14 (contact me for custom sizes)",
        "4x6, 8x12, 12x18, 16x24, 20x30, 24x36 (all print ready)",
        "16x20, 11x14, A2 — pick the one you love.",
    ],
)
def test_emptied_list_with_other_prose_is_a_hand_edit(line):
    assert fix(f"Intro.\n{line}\nOutro.") is None


def test_count_left_on_an_unedited_line_is_a_hand_edit():
    text = "Includes 8 print sizes.\n4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4"
    assert fix(text) is None
    # an untouched count is fine when only the disclosure sentence changes
    assert (
        fix("Includes 8 print sizes.\n4x5, 8x10\nThe photograph itself is the artist's " + OLD_TAIL)
        is not None
    )


@pytest.mark.parametrize(
    "text",
    [
        "Intro.\n8 different sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4",
        "Intro.\n8 printable sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4",
        "Intro.\nEight sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4",
        "Includes eight print sizes.\n4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4",
    ],
)
def test_counts_written_other_ways_are_a_hand_edit(text):
    assert fix(text) is None


def test_count_noun_kept_as_written():
    out = fix("Intro.\n8 size options: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4")
    assert out == "Intro.\n5 size options: 4x5, 5x7, 8x10, A3, A4"


@pytest.mark.parametrize(
    "text",
    [
        "Sizes included:\n16x20, 11x14\nEnjoy!",
        "Sizes:\n✔ 16x20\n✔ 11x14",
    ],
)
def test_intro_line_whose_list_vanished_is_a_hand_edit(text):
    assert fix(text) is None


def test_empty_result_is_a_hand_edit():
    assert fix("16x20, 11x14") is None
    assert fix("✔ 16x20\n") is None


@pytest.mark.parametrize(
    "line",
    [
        # a stray N (the run length) elsewhere means the count may be stated twice
        "Printable in 3 sizes: 5x7, 11x14, 16x20 (2:3 ratio, 3 files, 300 PPI)",
        "Includes 8x10 and 16x20, 2 sizes, $2 off",
        "A3, A2 - 2 sizes. Step 2: print.",
        "Eight (8) sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4",
        "Sizes (8): 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4",
        "Sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4 - 8 total",
        "A dozen sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4, 4x6, 8x12, 12x18, 16x24",
        "You get 8 JPG files in 8 sizes: 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4",
    ],
)
def test_stray_run_length_elsewhere_is_a_hand_edit(line):
    assert fix(f"Intro.\n{line}") is None


def test_rewritten_count_alone_is_fine_and_blank_lines_do_not_hide_a_vanished_list():
    out = fix("Intro.\n✔ 8 print sizes, one per frame (4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4)")
    assert out == "Intro.\n✔ 5 print sizes, one per frame (4x5, 5x7, 8x10, A3, A4)"
    assert fix("Sizes:\n\n11x14, 16x20, A2\n\nThanks") is None


EIGHT = "4x5, 5x7, 8x10, 11x14, 16x20, A2, A3, A4"


@pytest.mark.parametrize(
    "text",
    [
        "16x20, A3, A4 with 5 files",  # a number inside an edited line
        "Sizes (4):\n✔ 4x5\n✔ 8x10\n✔ 16x20\n✔ 11x14",
        "Sizes (8): 4x5, 5x7, 8x10, 11x14,\n16x20, A2, A3, A4",
        "Total of 8: 4x5, 5x7, 8x10, A3\nand 11x14, 16x20, A2, A4",
        "Multiple sizes: 4x5, 16x20",
        f"VIII sizes: {EIGHT}",
        f"A dozen sizes: {EIGHT}",
        "Includes:\n5 files at 300 PPI\n4x5, 16x20",  # a number in the edited block
    ],
)
def test_numbers_in_the_scope_of_an_edit_are_a_hand_edit(text):
    assert fix(f"Intro.\n\n{text}") is None


def test_numbers_elsewhere_do_not_block_a_fixable_size_line():
    text = (
        "Delivered as 5 files at 300 PPI, ready in 2 minutes.\n"
        "\n"
        "Misty Ridge at dawn.\n"
        "\n"
        "Includes these print sizes:\n"
        "4x5 · 8x10 · 16x20 · A3\n"
        "\n"
        "Thanks for visiting!"
    )
    assert fix(text) == text.replace("4x5 · 8x10 · 16x20 · A3", "4x5 · 8x10 · A3")


def test_ratio_in_the_heading_is_not_a_count():
    text = "Intro.\n\nPRINTABLE SIZES (2:3 Ratio):\n4x5, 8x10, 16x20\n\nThanks"
    assert fix(text) == "Intro.\n\nPRINTABLE SIZES (2:3 Ratio):\n4x5, 8x10\n\nThanks"
    assert fix("Intro.\n\nPrint Sizes (3:2 ratio)\n4x5 · 16x20 · A3") == (
        "Intro.\n\nPrint Sizes (3:2 ratio)\n4x5 · A3"
    )


def test_ratio_prefix_line_is_fixed_but_a_count_before_a_list_is_not_a_ratio():
    assert fix("Intro.\n\n2:3 ratio prints: 4x5, 16x20, 8x10") == (
        "Intro.\n\n2:3 ratio prints: 4x5, 8x10"
    )


def test_resolution_next_to_the_list_is_not_a_count():
    text = "Intro.\n\n✔ 300 DPI, sRGB\n✔ Sizes: 4x5, 8x10, 16x20\n\nThanks"
    assert fix(text) == "Intro.\n\n✔ 300 DPI, sRGB\n✔ Sizes: 4x5, 8x10\n\nThanks"
    # a bare number is still a possible count
    assert fix("Intro.\n\n✔ 300 files\n✔ Sizes: 4x5, 8x10, 16x20\n\nThanks") is None


def test_count_never_starts_inside_a_ratio():
    out = fix("Intro.\n\n4:5 sizes: 4x5, 8x10, 16x20, 11x14, A2")
    assert out is None or "4:5" in out


def test_ratio_overlapping_a_size_token_is_not_exempt():
    assert fix("Intro.\n\nTotal 4: 4 x 5, 8 x 10, 16 x 20, 11 x 14") is None


@pytest.mark.parametrize("after", ["All 4 print beautifully.", "That is 4 in total!"])
def test_number_in_the_next_block_is_a_hand_edit(after):
    assert fix(f"Sizes:\n✔ 4x5\n✔ 8x10\n✔ 16x20\n✔ 11x14\n\n{after}") is None
