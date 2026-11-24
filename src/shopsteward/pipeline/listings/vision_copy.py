"""Vision-for-copy: score each valid landing winner with the vision adapter and
emit a `photo.scored` event (keyed by its catalog photo_id, else the synthetic
id file-<file_id[:12]>) so listing copy regains subject/style/risk signals.
Copy helper only -- never rejects or edits a photo. Gated + cost-capped;
offline default is the fixture adapter.

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


def already_scored(conn: sqlite3.Connection, user_id: int) -> set[str]:
    done = set()
    for e in read_all(conn, "photo.scored"):
        if e.user_id == user_id:
            done.add(e.payload.get("photo_id"))
    return done


def score_photo(
    conn: sqlite3.Connection,
    user_id: int,
    photo_id: str,
    path: str,
    *,
    adapter: VisionAdapter,
    model: str,
) -> bool:
    """Score ONE file and append its `llm.call` + `photo.scored` events. False
    (nothing appended) on a vision failure -- non-fatal, callers count it.
    Shared by `run_vision_copy` and `catalog_expand.expand_one`."""
    try:
        result = adapter.score_commercial(_read_bytes(path), model=model)
    except Exception:  # noqa: BLE001 - per-photo vision failure is non-fatal
        # A failed vision call is unledgered: its cost is unknown. Callers bound
        # the spend (catalog_expand's attempt cap).
        return False
    usage = result.usage
    if usage is not None:
        append(
            conn,
            Event(
                user_id=user_id,
                type="llm.call",
                payload={
                    "feature": "vision_copy",
                    "model": usage.model,
                    "est_cost_usd": usage.est_cost_usd,
                },
            ),
        )
    append(
        conn,
        Event(
            user_id=user_id,
            type="photo.scored",
            payload={
                "photo_id": photo_id,
                "composite": 0.0,
                "scores": {},
                "vision": {"triage": {"verdict": result.verdict.model_dump()}},
            },
        ),
    )
    return True


def run_vision_copy(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    adapter: VisionAdapter,
    model: str,
    soft_cap_usd: float,
    month_prefix: str,
    regenerate: bool = False,
) -> dict:
    rebuild_pipeline(conn)
    # Catalog-matched files (photo_id set) need copy signals too: with no
    # photo.scored for that photo_id, copy got subject "unspecified" and wrote
    # a generic landscape for a bear or a bison (2026-11-22).
    rows = conn.execute(
        "SELECT file_id, path, photo_id FROM proj_landing_files "
        "WHERE user_id=? AND status='valid' ORDER BY file_id",
        (user_id,),
    ).fetchall()
    done = set() if regenerate else already_scored(conn, user_id)

    scored = skipped = failed = 0
    cap_hit = False
    for row in rows:
        sid = row["photo_id"] or _synthetic_id(row["file_id"])
        if sid in done:
            skipped += 1
            continue
        if monthly_spend(conn, user_id, month_prefix) >= soft_cap_usd:
            cap_hit = True
            break
        if not score_photo(conn, user_id, sid, row["path"], adapter=adapter, model=model):
            failed += 1
            continue
        scored += 1
        done.add(sid)  # two files matched to one photo_id: pay once

    rebuild_pipeline(conn)
    return {"scored": scored, "skipped": skipped, "failed": failed, "cap_hit": cap_hit}
