"""Tripwire, not a functional test: guards `tests/fixtures/pinterest/*.json`
against the two ways a Pinterest fixture can violate "this repo is public"
(design doc `docs/designs/2026-08-24-pinterest-adapter-and-loop-roadmap.md`
§1.3 -- no real board/pin/account id in a fixture; CLAUDE.md -- never commit
a photo file, which `PinMedia.data_b64` (a base64-encoded mockup) would be).

No fixtures exist yet, so this passes trivially today. It exists so that
whoever records a live fixture at 11pm hits a failure with an actionable
message instead of silently committing a live shop id or a multi-KB image
blob.
"""

import json
import re
from pathlib import Path

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "pinterest"

# fake.py mints "board-{n}" / "pin-{n}"; a recorded fixture must use the same
# synthetic shape, never a real Pinterest numeric id.
SYNTHETIC_ID_RE = re.compile(r"^(board|pin)-\d+$")

# L5 (guardrail review, 2026-08-25): the real Pinterest v5 API names its own
# id field plain "id" (board_id/pin_id are this repo's OWN synthetic naming,
# `fake.py`'s), so a raw recorded response is the most likely real fixture to
# slip past a check that only looks for "board_id"/"pin_id" literally.
ID_KEYS = ("board_id", "pin_id", "id")

# A real base64-encoded image is many KB; a synthetic 1x1 PNG placeholder
# is well under this. Generous margin, not a byte-exact check.
MAX_DATA_B64_LEN = 200

# Live identifiers that must never appear in a Pinterest fixture.
BANNED_SUBSTRINGS = ["52644245"]  # the live Etsy shop id

HYGIENE_HELP = (
    "\n\nPinterest fixture hygiene violation in {path}.\n"
    "This repo is public -- fixtures must be synthetic:\n"
    "  - board_id/pin_id must match the fake's pattern (board-{{n}} / pin-{{n}}),"
    " never a real Pinterest id.\n"
    "  - data_b64 must be a tiny synthetic placeholder (e.g. a 1x1 PNG), never a"
    " real composited mockup's bytes.\n"
    "  - no live identifiers (e.g. Etsy shop id 52644245) anywhere in the file.\n"
    "Scrub the fixture and re-record with synthetic ids/a 1x1 PNG before committing."
)


def _fixture_files() -> list[Path]:
    if not FIXTURE_DIR.is_dir():
        return []
    return sorted(FIXTURE_DIR.glob("*.json"))


def _walk(obj: object):
    """Yield every (key, value) pair found anywhere in a JSON structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def test_data_b64_is_a_tiny_placeholder_not_a_real_image() -> None:
    for path in _fixture_files():
        data = json.loads(path.read_text())
        for key, value in _walk(data):
            if key == "data_b64" and isinstance(value, str):
                assert len(value) <= MAX_DATA_B64_LEN, HYGIENE_HELP.format(path=path)


def test_board_and_pin_ids_are_synthetic() -> None:
    # L5 (guardrail review, 2026-08-25): widened on two axes -- (1) also
    # checks the real Pinterest v5 field name "id", not just this repo's own
    # "board_id"/"pin_id" synthetic naming (a raw recorded response is the
    # most likely real fixture to slip through otherwise), and (2) a NON-str
    # value (a real numeric Pinterest id, recorded as a JSON number) is now
    # an automatic failure rather than silently skipped by the old
    # `isinstance(value, str)` guard.
    for path in _fixture_files():
        data = json.loads(path.read_text())
        for key, value in _walk(data):
            if key not in ID_KEYS:
                continue
            assert isinstance(value, str) and SYNTHETIC_ID_RE.match(value), HYGIENE_HELP.format(
                path=path
            )


def test_no_live_identifiers_in_fixture_text() -> None:
    for path in _fixture_files():
        text = path.read_text()
        for banned in BANNED_SUBSTRINGS:
            assert banned not in text, HYGIENE_HELP.format(path=path)
