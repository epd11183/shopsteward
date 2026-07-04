"""Wraps a VisionAdapter: prepares a downscaled/scrubbed JPEG, calls
score_commercial with the triage or rescore model, and returns the verdict +
usage in ScorerResult.detail (orchestrator turns usage into llm.call events)."""

from shopsteward.pipeline.imaging import prep_vision_jpeg
from shopsteward.pipeline.scorers import ScoreContext, ScorerResult, register


class CommercialScorer:
    name = "commercial"

    def score(self, ctx: ScoreContext) -> ScorerResult | None:
        vision_cfg = ctx.profile.vision
        model = vision_cfg.rescore_model if ctx.rescore else vision_cfg.triage_model
        jpeg_bytes = prep_vision_jpeg(ctx.jpeg_path, vision_cfg.max_long_edge_px)
        result = ctx.vision.score_commercial(jpeg_bytes, model=model)
        return ScorerResult(
            score=float(result.verdict.commercial_score),
            detail={
                "verdict": result.verdict.model_dump(),
                "usage": result.usage.model_dump() if result.usage is not None else None,
                "model": model,
            },
        )


register(CommercialScorer())
