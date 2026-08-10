from pathlib import Path

import numpy as np
import pytest

from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.adapters.look.interface import LookParseError
from shopsteward.core.db import connect, migrate
from shopsteward.editing.edit import run_edit
from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder
from shopsteward.editing.xmp import sidecar_path

USER = 1
KNOBS = {
    "exposure_target_luma": 0.45, "exposure_max_stops": 1.5, "shadow_trigger_luma": 0.12,
    "shadow_lift_max": 1.0, "shadow_range_low": 0, "shadow_range_high": 45,
    "cast_trigger": 0.06, "cast_nudge_cap": 8, "cast_full_scale_bias": 0.2,
}


def _folder_with_raws(tmp_path: Path, names: list[str]) -> tuple[Path, FakeRawDecoder]:
    images = {}
    for i, name in enumerate(names):
        raw = tmp_path / name
        raw.write_bytes(f"raw-{name}".encode())
        img = np.full((8, 8, 3), 0.1 + 0.1 * i, dtype=np.float32)
        images[str(raw)] = DecodedImage(rgb=img)
    return tmp_path, FakeRawDecoder(images)


def _conn():
    c = connect(":memory:")
    migrate(c)
    return c


def test_run_edit_writes_a_sidecar_per_raw(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3", "B.CR3"])
    report = run_edit(_conn(), USER, folder, "bright-and-true",
                      decoder=decoder, look_adapter=FixtureLookAdapter(),
                      model="m", knobs=KNOBS, regenerate=False, overwrite=False, batch_lock=False)
    assert report.written == 2
    assert sidecar_path(tmp_path / "A.CR3").exists()
    assert sidecar_path(tmp_path / "B.CR3").exists()


def test_run_edit_skips_existing_without_overwrite(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3"])
    sidecar_path(tmp_path / "A.CR3").write_text("existing")
    report = run_edit(_conn(), USER, folder, "bright-and-true",
                      decoder=decoder, look_adapter=FixtureLookAdapter(),
                      model="m", knobs=KNOBS, regenerate=False, overwrite=False, batch_lock=False)
    assert report.written == 0 and report.skipped_existing == 1


def test_run_edit_fails_fast_on_look_error_before_writing(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3"])

    class Boom:
        def generate_look(self, description, *, model):
            raise LookParseError("boom")

    with pytest.raises(LookParseError):
        run_edit(_conn(), USER, folder, "some new look",
                 decoder=decoder, look_adapter=Boom(),
                 model="m", knobs=KNOBS, regenerate=False, overwrite=False, batch_lock=False)
    assert not sidecar_path(tmp_path / "A.CR3").exists()  # nothing written


def test_batch_lock_applies_same_exposure_to_all(tmp_path):
    folder, decoder = _folder_with_raws(tmp_path, ["A.CR3", "B.CR3"])
    run_edit(_conn(), USER, folder, "bright-and-true",
             decoder=decoder, look_adapter=FixtureLookAdapter(),
             model="m", knobs=KNOBS, regenerate=False, overwrite=False, batch_lock=True)
    a = sidecar_path(tmp_path / "A.CR3").read_text()
    b = sidecar_path(tmp_path / "B.CR3").read_text()
    def _exp(x): return x.split('crs:Exposure2012="')[1].split('"')[0]
    assert _exp(a) == _exp(b)
