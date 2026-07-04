"""Placeholder scorers wired into the registry with weight 0 in the default
tuning profile. They return None (excluded from composite) until a real
implementation lands; keeping them registered documents the intended scoring
surface without adding cost or a fourth human touchpoint."""

from shopsteward.pipeline.scorers import ScoreContext, ScorerResult, register


class CatalogGapScorer:
    name = "catalog_gap"

    def score(self, ctx: ScoreContext) -> ScorerResult | None:
        return None


class HistoricalConversionScorer:
    name = "historical_conversion"

    def score(self, ctx: ScoreContext) -> ScorerResult | None:
        return None


register(CatalogGapScorer())
register(HistoricalConversionScorer())
