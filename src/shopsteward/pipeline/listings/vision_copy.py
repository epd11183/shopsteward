"""Vision-for-copy: score each photo-less landing winner with the vision adapter
and emit a `photo.scored` event (synthetic id file-<file_id[:12]>) so listing
copy regains subject/style/risk signals. Copy helper only -- never rejects or
edits a photo. Gated + cost-capped; offline default is the fixture adapter.

`composite` is emitted as 0.0: proj_scores.composite is NOT NULL and this
path never scores/gates a photo, so it carries no real composite score."""

import sqlite3
from pathlib import Path

from shopsteward.adapters.vision.interface import VisionAdapter
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.llm_ledger import monthly_spend
from shopsteward.pipeline.projections import rebuild_pipeline


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _synthetic_id(file_id: str) -> str:
    return f"file-{file_id[:12]}"


def _already_scored(conn: sqlite3.Connection, user_id: int) -> set[str]:
    done = set()
    for e in read_all(conn, "photo.scored"):
        if e.user_id == user_id:
            done.add(e.payload.get("photo_id"))
    return done


def run_vision_copy(
    conn: sqlite3.Connection, user_id: int, *, adapter: VisionAdapter, model: str,
    soft_cap_usd: float, month_prefix: str, regenerate: bool = False,
) -> dict:
    rebuild_pipeline(conn)
    rows = conn.execute(
        "SELECT file_id, path FROM proj_landing_files "
        "WHERE user_id=? AND status='valid' AND photo_id IS NULL ORDER BY file_id",
        (user_id,),
    ).fetchall()
    done = set() if regenerate else _already_scored(conn, user_id)

    scored = skipped = failed = 0
    cap_hit = False
    for row in rows:
        sid = _synthetic_id(row["file_id"])
        if sid in done:
            skipped += 1
            continue
        if monthly_spend(conn, user_id, month_prefix) >= soft_cap_usd:
            cap_hit = True
            break
        try:
            result = adapter.score_commercial(_read_bytes(row["path"]), model=model)
        except Exception:  # noqa: BLE001 - per-photo vision failure is non-fatal
            failed += 1
            continue
        usage = result.usage
        if usage is not None:
            append(conn, Event(user_id=user_id, type="llm.call", payload={
                "feature": "vision_copy", "model": usage.model,
                "est_cost_usd": usage.est_cost_usd}))
        append(conn, Event(user_id=user_id, type="photo.scored", payload={
            "photo_id": sid, "composite": 0.0, "scores": {},
            "vision": {"triage": {"verdict": result.verdict.model_dump()}}}))
        scored += 1

    rebuild_pipeline(conn)
    return {"scored": scored, "skipped": skipped, "failed": failed, "cap_hit": cap_hit}
