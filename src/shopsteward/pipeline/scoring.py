"""Orchestrator: unscored awaiting_scoring photos -> scorers -> composite ->
borderline Pro escalation -> photo.scored/queued + llm.call (live only) +
photo.score_failed."""

import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

from shopsteward.adapters.vision.interface import VisionAdapter
from shopsteward.core.events import Event, append, read_all
from shopsteward.editing.projections import rebuild_editing
from shopsteward.pipeline import tuning
from shopsteward.pipeline.models import ScoringRunResult
from shopsteward.pipeline.projections import rebuild_pipeline
from shopsteward.pipeline.scorers import ScoreContext, get_registered

logger = logging.getLogger(__name__)


def run_scoring(
    conn: sqlite3.Connection,
    user_id: int,
    vision: VisionAdapter,
    *,
    limit: int | None = None,
    live: bool = False,
) -> ScoringRunResult:
    rebuild_editing(conn)
    rebuild_pipeline(conn)

    profile = tuning.get_profile(conn, user_id)
    weights = profile.scoring.weights
    threshold = profile.scoring.gate1_threshold
    band = profile.scoring.borderline_band
    soft_cap = profile.vision.monthly_soft_cap_usd

    candidates = _candidates(conn, user_id, limit)
    scorers = [s for s in get_registered() if weights.get(s.name, 0) > 0]
    month_prefix = datetime.now(UTC).strftime("%Y-%m")

    scored = queued = escalated = failed = 0
    cap_hit = False

    for row in candidates:
        if live:
            spend = _monthly_spend(conn, user_id, month_prefix)
            if spend >= soft_cap:
                cap_hit = True
                logger.warning(
                    "monthly vision soft cap reached (%.2f >= %.2f usd); stopping run early",
                    spend,
                    soft_cap,
                )
                break
            if spend >= 0.8 * soft_cap:
                logger.warning(
                    "monthly vision spend at %.0f%% of soft cap (%.2f / %.2f usd)",
                    100 * spend / soft_cap,
                    spend,
                    soft_cap,
                )

        ctx = ScoreContext(
            user_id=user_id,
            photo_id=row["photo_id"],
            base_name=row["base_name"],
            jpeg_path=row["jpeg_path"],
            profile=profile,
            vision=vision,
        )

        scores, details, scorer_failed = _run_scorers(conn, user_id, scorers, ctx, live=live)
        if scorer_failed:
            failed += 1
            continue

        composite = _composite(scores, weights)
        escalated_flag = False
        commercial_scorer = next((s for s in scorers if s.name == "commercial"), None)

        if (
            commercial_scorer is not None
            and scores.get("commercial") is not None
            and abs(composite - threshold) <= band
        ):
            rescore_ctx = replace(ctx, rescore=True)
            try:
                rescore_result = commercial_scorer.score(rescore_ctx)
            except Exception as exc:
                _append_score_failed(conn, user_id, row["photo_id"], "commercial", exc)
                failed += 1
                continue
            if rescore_result is not None:
                scores["commercial"] = rescore_result.score
                details["commercial_rescore"] = rescore_result.detail
                escalated_flag = True
                if live:
                    _maybe_append_llm_call(
                        conn,
                        user_id,
                        row["photo_id"],
                        "borderline_rescore",
                        rescore_result.detail,
                        profile.vision.provider,
                    )
                composite = _composite(scores, weights)

        vision_detail = {
            "triage": _vision_summary(details.get("commercial")),
            "rescore": _vision_summary(details.get("commercial_rescore")),
        }

        append(
            conn,
            Event(
                user_id=user_id,
                type="photo.scored",
                payload={
                    "photo_id": row["photo_id"],
                    "profile_name": profile.name,
                    "scores": {
                        "technical": scores.get("technical"),
                        "commercial": scores.get("commercial"),
                        "catalog_gap": scores.get("catalog_gap"),
                        "historical_conversion": scores.get("historical_conversion"),
                    },
                    "composite": composite,
                    "escalated": escalated_flag,
                    "vision": vision_detail,
                },
            ),
        )
        scored += 1
        if escalated_flag:
            escalated += 1

        if composite >= threshold:
            append(
                conn,
                Event(
                    user_id=user_id,
                    type="photo.queued",
                    payload={"photo_id": row["photo_id"], "composite": composite},
                ),
            )
            queued += 1

    rebuild_pipeline(conn)
    return ScoringRunResult(
        scored=scored, queued=queued, escalated=escalated, failed=failed, cap_hit=cap_hit
    )


def _candidates(conn: sqlite3.Connection, user_id: int, limit: int | None) -> list[sqlite3.Row]:
    query = (
        "SELECT photo_id, base_name, jpeg_path FROM proj_photos "
        "WHERE user_id = ? AND status = 'awaiting_scoring' "
        "AND photo_id NOT IN (SELECT photo_id FROM proj_scores WHERE user_id = ?) "
        "ORDER BY photo_id"
    )
    params: tuple = (user_id, user_id)
    if limit is not None:
        query += " LIMIT ?"
        params = (*params, limit)
    return conn.execute(query, params).fetchall()


def _monthly_spend(conn: sqlite3.Connection, user_id: int, month_prefix: str) -> float:
    total = 0.0
    for e in read_all(conn, "llm.call"):
        if e.user_id != user_id or not (e.created_at or "").startswith(month_prefix):
            continue
        cost = e.payload.get("est_cost_usd")
        if cost is not None:
            total += cost
    return total


def _run_scorers(
    conn: sqlite3.Connection,
    user_id: int,
    scorers: list,
    ctx: ScoreContext,
    *,
    live: bool,
) -> tuple[dict[str, float | None], dict[str, dict], bool]:
    scores: dict[str, float | None] = {}
    details: dict[str, dict] = {}
    for scorer in scorers:
        try:
            result = scorer.score(ctx)
        except Exception as exc:
            _append_score_failed(conn, user_id, ctx.photo_id, scorer.name, exc)
            return scores, details, True

        if result is None:
            scores[scorer.name] = None
            continue

        scores[scorer.name] = result.score
        details[scorer.name] = result.detail
        if live and scorer.name == "commercial":
            _maybe_append_llm_call(
                conn,
                user_id,
                ctx.photo_id,
                "commercial_triage",
                result.detail,
                ctx.profile.vision.provider,
            )

    return scores, details, False


def _append_score_failed(
    conn: sqlite3.Connection, user_id: int, photo_id: str, scorer_name: str, exc: Exception
) -> None:
    append(
        conn,
        Event(
            user_id=user_id,
            type="photo.score_failed",
            payload={
                "photo_id": photo_id,
                "scorer": scorer_name,
                "error": {"code": type(exc).__name__, "message": str(exc)},
            },
        ),
    )


def _maybe_append_llm_call(
    conn: sqlite3.Connection,
    user_id: int,
    photo_id: str,
    purpose: str,
    detail: dict,
    provider: str,
) -> None:
    usage = detail.get("usage")
    if not usage:
        return
    append(
        conn,
        Event(
            user_id=user_id,
            type="llm.call",
            payload={
                "provider": provider,
                "model": detail.get("model"),
                "purpose": purpose,
                "photo_id": photo_id,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "est_cost_usd": usage.get("est_cost_usd"),
            },
        ),
    )


def _composite(scores: dict[str, float | None], weights: dict[str, float]) -> float:
    weighted_sum = 0.0
    total_weight = 0.0
    for name, weight in weights.items():
        if weight <= 0:
            continue
        score = scores.get(name)
        if score is None:
            continue
        weighted_sum += weight * score
        total_weight += weight
    if total_weight == 0:
        return 0.0
    composite = weighted_sum / total_weight
    return round(max(0.0, min(100.0, composite)), 1)


def _vision_summary(detail: dict | None) -> dict | None:
    if detail is None:
        return None
    return {"model": detail.get("model"), "verdict": detail.get("verdict")}
