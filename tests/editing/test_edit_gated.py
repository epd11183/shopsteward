from pathlib import Path

import numpy as np

from shopsteward.adapters.look.fake import FakeLookAdapter
from shopsteward.adapters.look.interface import LookProfile, LookResult, LookUsage
from shopsteward.core.db import connect, migrate
from shopsteward.editing.edit import run_edit
from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

USER = 1
KNOBS = {
    "exposure_target_luma": 0.4,
    "exposure_max_stops": 1.5,
    "shadow_trigger_luma": 0.12,
    "shadow_lift_max": 0.8,
    "shadow_range_low": 0,
    "shadow_range_high": 35,
}
GUARD = {
    "max_saturation_load": 220,
    "max_contrast_tone": 140,
    "max_presence_load": 200,
    "max_split_saturation": 60,
}


def test_run_edit_forwards_guard_and_ledgers(tmp_path: Path):
    raw = tmp_path / "A.CR3"
    raw.write_bytes(b"a")
    decoder = FakeRawDecoder({str(raw): DecodedImage(rgb=np.full((8, 8, 3), 0.3, np.float32))})
    adapter = FakeLookAdapter(
        [
            LookResult(
                profile=LookProfile(name="t", contrast=15),
                usage=LookUsage(model="m", est_cost_usd=0.01),
            )
        ]
    )
    conn = connect(":memory:")
    migrate(conn)
    report = run_edit(
        conn,
        USER,
        tmp_path,
        "some described look",
        decoder=decoder,
        look_adapter=adapter,
        model="m",
        knobs=KNOBS,
        regenerate=False,
        overwrite=False,
        batch_lock=False,
        guard_knobs=GUARD,
        soft_cap_usd=5.0,
        month_prefix="2026-08",
    )
    assert report.written == 1
    from shopsteward.editing.look_cost import month_look_cost

    assert month_look_cost(conn, USER, "2026-08") == 0.01
