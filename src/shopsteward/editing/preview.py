"""A/B look preview: write a candidate look and a comparison seed as sidecars
into <sample_dir>/_preview/<look>/ subfolders (copied RAWs + sidecars) so the
operator can import both into Lightroom and compare. Correction is identical
across both, isolating the look. Non-destructive: writes only under _preview/."""

import shutil
import sqlite3
from pathlib import Path

from shopsteward.adapters.look.interface import LookAdapter
from shopsteward.editing import looks
from shopsteward.editing.analyze import analyze_raw
from shopsteward.editing.ingest import RAW_SUFFIXES
from shopsteward.editing.rawdecode import RawDecoder
from shopsteward.editing.xmp import compose, write_sidecar


def run_preview(
    conn: sqlite3.Connection, user_id: int, sample_dir: Path, look_arg: str, *,
    against: str, decoder: RawDecoder, look_adapter: LookAdapter, model: str,
    knobs: dict, looks_dir: Path, **resolve_kwargs,
) -> dict:
    looks.seed(conn, user_id, looks_dir)
    candidate = looks.resolve_look(conn, user_id, look_arg, look_adapter,
                                   model=model, regenerate=False, **resolve_kwargs)
    seed = looks.get_look(conn, user_id, against)

    raws = sorted(p for p in Path(sample_dir).iterdir()
                  if p.is_file() and p.suffix.lower() in RAW_SUFFIXES)
    preview_root = Path(sample_dir) / "_preview"
    for label, look in ((candidate.name, candidate), (seed.name, seed)):
        sub = preview_root / label
        sub.mkdir(parents=True, exist_ok=True)
        for rp in raws:
            correction = analyze_raw(decoder.decode(str(rp)), knobs)
            dest = sub / rp.name
            shutil.copy2(rp, dest)
            write_sidecar(dest, compose(correction, look), overwrite=True)
    return {"candidate": candidate.name, "against": seed.name,
            "frames": len(raws), "dir": str(preview_root)}
