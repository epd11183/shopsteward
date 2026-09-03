import pytest
from PIL import Image

from shopsteward.pipeline.listings.images import (
    SellableFileTooLargeError,
    order_listing_images,
    resolve_sellable_file,
)
from shopsteward.pipeline.listings.models import ListingConfig

_CFG_BASE = {
    "schema": "shopsteward.listing/1",
    "name": "default",
    "copy": {
        "provider": "openrouter",
        "model": "m",
        "ab_alternate": "m2",
        "temperature": 0.4,
        "append_disclosure": True,
        "prompt_path": "p.txt",
        "house_style_path": "h.md",
        "est_cost_per_mtok": {},
    },
    "pricing": {
        "currency": "USD",
        "digital_quantity": 999,
        "formats": {"digital_download": {"base_price": 12.0, "margin_floor": 6.0}},
        "etsy_fees": {
            "listing_fee": 0.20,
            "transaction_pct": 0.065,
            "payment_pct": 0.03,
            "payment_flat": 0.25,
        },
    },
    "image_order": [
        "single",
        "framed_poster",
        "canvas_edge",
        "acrylic",
        "gallery_wall",
        "digital_whatyougot",
    ],
    "image_cap": 10,
    "etsy": {
        "who_made": "i_did",
        "when_made": "2020_2026",
        "is_supply": False,
        "taxonomy_id": 0,
        "should_auto_renew": True,
        "sellable_max_bytes": 20_000_000,
    },
}


def make_cfg(**overrides) -> ListingConfig:
    data = {**_CFG_BASE, **overrides}
    return ListingConfig.model_validate(data)


def test_order_listing_images_hero_single_first():
    mockups = [
        {"path": "/m/framed.jpg", "intent": "framed_poster"},
        {"path": "/m/single.jpg", "intent": "single"},
        {"path": "/m/wyg.jpg", "intent": "digital_whatyougot"},
    ]
    images = order_listing_images(mockups, make_cfg())
    assert images[0].intent == "single"
    assert images[0].rank == 1
    assert {img.intent for img in images} == {"framed_poster", "single", "digital_whatyougot"}
    assert [img.rank for img in images] == [1, 2, 3]


def test_order_listing_images_digital_whatyougot_included():
    mockups = [{"path": "/m/wyg.jpg", "intent": "digital_whatyougot"}]
    images = order_listing_images(mockups, make_cfg())
    assert len(images) == 1
    assert images[0].intent == "digital_whatyougot"


def test_order_listing_images_capped():
    mockups = [{"path": f"/m/{i}.jpg", "intent": "single"} for i in range(15)]
    images = order_listing_images(mockups, make_cfg(image_cap=10))
    assert len(images) == 10
    assert [img.rank for img in images] == list(range(1, 11))


def test_order_listing_images_unknown_intent_sorts_last():
    mockups = [
        {"path": "/m/unknown.jpg", "intent": "mystery"},
        {"path": "/m/single.jpg", "intent": "single"},
    ]
    images = order_listing_images(mockups, make_cfg())
    assert [img.intent for img in images] == ["single", "mystery"]


def test_resolve_sellable_file_landing_original_for_small_jpeg(tmp_path):
    path = tmp_path / "hero.jpg"
    Image.new("RGB", (100, 100), (10, 20, 30)).save(path, "JPEG")
    sellable = resolve_sellable_file(str(path), sellable_max_bytes=20_000_000)
    assert sellable.source == "landing_original"
    assert sellable.bytes == path.stat().st_size
    assert len(sellable.sha256) == 64


def test_resolve_sellable_file_derives_jpeg_for_tiff(tmp_path):
    path = tmp_path / "hero.tif"
    Image.new("RGB", (100, 100), (10, 20, 30)).save(path, "TIFF")
    sellable = resolve_sellable_file(str(path), sellable_max_bytes=20_000_000)
    assert sellable.source == "derived_jpeg"


def _noisy_jpeg(path, size=(400, 400)) -> None:
    # Random pixel noise resists compression, so it behaves like a real
    # oversized photo (unlike a flat color, which q100 already compresses
    # to almost nothing) -- needed to exercise the "still over the cap at
    # q100" path this bug lived in.
    import os

    width, height = size
    pixels = os.urandom(width * height * 3)
    Image.frombytes("RGB", size, pixels).save(path, "JPEG", quality=100)


def test_resolve_sellable_file_derives_jpeg_when_over_limit(tmp_path):
    # Mutation-relevant case the original test lacked: it used
    # sellable_max_bytes=1 (impossible at any quality) and only asserted
    # source == "derived_jpeg", never that the derived bytes actually fit
    # under the cap -- so the quality=100 no-op re-encode bug would have
    # passed it.
    path = tmp_path / "hero.jpg"
    _noisy_jpeg(path)
    at_q100 = path.stat().st_size
    cap = at_q100 // 2
    sellable = resolve_sellable_file(str(path), sellable_max_bytes=cap)
    assert sellable.source == "derived_jpeg"
    assert sellable.bytes <= cap


def test_resolve_sellable_file_raises_when_floor_quality_still_too_big(tmp_path):
    path = tmp_path / "hero.jpg"
    _noisy_jpeg(path)
    with pytest.raises(SellableFileTooLargeError):
        resolve_sellable_file(str(path), sellable_max_bytes=100)


def test_resolve_sellable_file_deterministic(tmp_path):
    path = tmp_path / "hero.jpg"
    Image.new("RGB", (100, 100), (10, 20, 30)).save(path, "JPEG")
    first = resolve_sellable_file(str(path), sellable_max_bytes=20_000_000)
    second = resolve_sellable_file(str(path), sellable_max_bytes=20_000_000)
    assert first.sha256 == second.sha256
