import numpy as np

from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder


def test_fake_decoder_returns_decoded_image():
    img = np.zeros((4, 4, 3), dtype=np.float32)
    dec = FakeRawDecoder(
        {"a.CR3": DecodedImage(rgb=img, wb_multipliers=(2.0, 1.0, 1.5, 0.0), exif={"Model": "R5"})}
    )
    out = dec.decode("a.CR3")
    assert out.rgb.shape == (4, 4, 3)
    assert out.wb_multipliers[0] == 2.0
    assert out.exif["Model"] == "R5"


def test_decoded_image_xyz_matrix_defaults_to_none():
    img = np.zeros((4, 4, 3), dtype=np.float32)
    assert DecodedImage(rgb=img).xyz_matrix is None
