"""Locks the seam the unit tests missed: run_edit emits events (RAW-only, so
jpeg_path is None) and rebuild_editing must consume them without crashing."""

from pathlib import Path

import numpy as np

from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.editing.config import load_correction_knobs
from shopsteward.editing.edit import run_edit
from shopsteward.editing.projections import rebuild_editing
from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

USER = 1


def test_run_edit_then_rebuild_editing_does_not_crash(tmp_path: Path):
    raw = tmp_path / "IMG_1.CR3"
    raw.write_bytes(b"raw-IMG_1.CR3")
    decoder = FakeRawDecoder({str(raw): DecodedImage(rgb=np.full((8, 8, 3), 0.2, np.float32))})

    conn = connect(":memory:")
    migrate(conn)
    report = run_edit(
        conn, USER, tmp_path, "bright-and-true",
        decoder=decoder, look_adapter=FixtureLookAdapter(),
        model="m", knobs=load_correction_knobs(),
        regenerate=False, overwrite=False, batch_lock=False,
    )
    assert report.written == 1

    # This is the seam that crashed before jpeg_path was made nullable.
    rebuild_editing(conn)
    row = conn.execute(
        "SELECT jpeg_path, status FROM proj_photos WHERE user_id=?", (USER,)
    ).fetchone()
    assert row is not None
    assert row["jpeg_path"] is None  # RAW-only: no paired JPEG
