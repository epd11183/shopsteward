"""OpenRouter planner adapter. httpx only -- no vendor SDK (PRD §13 decisions
22, 36). NOT wired to any default path; live use is triple-gated (flag + env
+ key) by the caller, mirroring OpenRouterCopyAdapter.

Free-text response -- no JSON schema is needed, this is prose narration of
the deterministic Brief, never structured data the pipeline parses."""

import json

import httpx

from shopsteward.adapters.planner.interface import (
    PlannerNarration,
    PlannerParseError,
    PlannerUsage,
)

BASE = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM_PROMPT = (
    "You are the shop's business manager. Interpret the brief below for the "
    "artist-owner in a few plain sentences: what changed, what's working, "
    "what needs attention, and why. Cite the actual figures from the brief. "
    "DO NOT invent any number or listing that is not in the brief."
)

_MAX_ERROR_LEN = 500


class OpenRouterPlannerAdapter:
    def __init__(
        self,
        model: str,
        api_key: str,
        est_cost_per_mtok: dict[str, dict[str, float]] | None = None,
        timeout: float = 60.0,
    ):
        self._model = model
        self._est_cost_per_mtok = est_cost_per_mtok
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/epd11183/shopsteward",
                "X-Title": "ShopSteward",
            },
            timeout=timeout,
        )

    def narrate(self, deterministic_brief_text: str) -> PlannerNarration:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": deterministic_brief_text},
            ],
        }
        resp = self._client.post(BASE, json=body)
        resp.raise_for_status()

        try:
            payload = resp.json()
            text = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise PlannerParseError(
                f"could not parse OpenRouter response: {resp.text[:_MAX_ERROR_LEN]!r}"
            ) from exc

        return PlannerNarration(text=text, usage=self._build_usage(payload))

    def _build_usage(self, payload: dict) -> PlannerUsage:
        meta = payload.get("usage", {})
        prompt_tokens = meta.get("prompt_tokens", 0) or 0
        completion_tokens = meta.get("completion_tokens", 0) or 0

        est_cost_usd = 0.0
        if self._est_cost_per_mtok and self._model in self._est_cost_per_mtok:
            rates = self._est_cost_per_mtok[self._model]
            est_cost_usd = (prompt_tokens / 1e6) * rates["in"] + (completion_tokens / 1e6) * rates[
                "out"
            ]

        return PlannerUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            est_cost_usd=est_cost_usd,
        )
