"""Smallest test that proves the Pinterest fake (design doc §1, "smallest
test that proves it"): create a board, create a pin against it, `list_pins`
returns it, `delete_pin` removes it, a second `delete_pin` raises
`PinterestWriteError(404)`, and `pin_analytics` for a window with no data
returns `metrics == {}` (not zeros)."""

from datetime import date

import pytest

from shopsteward.adapters.pinterest.fake import FakePinterestWriteAdapter
from shopsteward.adapters.pinterest.interface import PinterestWriteError
from shopsteward.adapters.pinterest.models import (
    BoardSpec,
    PinMedia,
    PinMetric,
    PinSpec,
)


def test_create_board_then_pin_then_list_then_delete() -> None:
    adapter = FakePinterestWriteAdapter()

    board_ref = adapter.create_board(BoardSpec(name="Nature & Landscape Wall Art"))
    assert board_ref.board_id in adapter.boards

    pin_ref = adapter.create_pin(
        PinSpec(
            board_id=board_ref.board_id,
            media=PinMedia(source_type="image_url", url="https://example.com/img.jpg"),
            link="https://www.etsy.com/listing/123",
            title="Sandhill Cranes at Dawn",
            description="Fine art print",
        )
    )
    assert pin_ref.pin_id in adapter.pins
    assert adapter.pins[pin_ref.pin_id].board_id == board_ref.board_id

    adapter.delete_pin(pin_ref.pin_id)
    assert pin_ref.pin_id not in adapter.pins

    with pytest.raises(PinterestWriteError) as exc_info:
        adapter.delete_pin(pin_ref.pin_id)
    assert exc_info.value.status_code == 404


def test_pin_analytics_absent_metric_is_not_zero(tmp_path) -> None:
    from shopsteward.adapters.pinterest.fake import FixturePinterestAdapter

    (tmp_path / "pin_analytics.json").write_text("{}")
    reader = FixturePinterestAdapter(tmp_path)

    result = reader.pin_analytics(
        "pin-1",
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
        metrics=[PinMetric.IMPRESSION, PinMetric.OUTBOUND_CLICK],
    )
    assert result.metrics == {}
