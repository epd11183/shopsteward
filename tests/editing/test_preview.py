from pathlib import Path

import numpy as np

from shopsteward.adapters.look.fake import FixtureLookAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.editing.config import LOOKS_DIR, load_correction_knobs
from shopsteward.editing.preview import run_preview
from shopsteward.editing.rawdecode import DecodedImage, FakeRawDecoder

USER = 1


def test_preview_writes_candidate_and_seed(tmp_path: Path):
    raw = tmp_path / "A.CR3"
    raw.write_bytes(b"a")
    decoder = FakeRawDecoder({str(raw): DecodedImage(rgb=np.full((8, 8, 3), 0.3, np.float32))})
    conn = connect(":memory:")
    migrate(conn)
    out = run_preview(conn, USER, tmp_path, "bright-and-true", against="national-geographic",
                      decoder=decoder, look_adapter=FixtureLookAdapter(), model="fixture",
                      knobs=load_correction_knobs(), looks_dir=LOOKS_DIR)
    assert (tmp_path / "_preview" / "bright-and-true" / "A.xmp").exists()
    assert (tmp_path / "_preview" / "national-geographic" / "A.xmp").exists()
    assert out["candidate"] == "bright-and-true"
