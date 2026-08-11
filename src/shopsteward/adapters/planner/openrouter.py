"""OpenRouter planner adapter. httpx only -- no vendor SDK (PRD §13 decisions
22, 36). NOT wired to any default path; live use is triple-gated (flag + env
+ key) by the caller, mirroring OpenRouterCopyAdapter.

narrate() is free-text (prose narration of the deterministic Brief). plan()
uses OpenRouter `response_format: json_schema, strict: true` (the
`adapters/copy/openrouter.py` pattern) -- its output is structured data the
pipeline-side validation gate parses and re-grounds, never trusted as-is."""

import json

import httpx
from pydantic import ValidationError

from shopsteward.adapters.planner.interface import (
    CapabilityDescriptor,
    PlannerNarration,
    PlannerParseError,
    PlannerPlan,
    PlannerUsage,
    ProposalIntent,
)

BASE = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM_PROMPT = (
    "You are the shop's business manager. Interpret the brief below for the "
    "artist-owner in a few plain sentences: what changed, what's working, "
    "what needs attention, and why. Cite the actual figures from the brief. "
    "DO NOT invent any number or listing that is not in the brief."
)

_PLAN_SYSTEM_PROMPT = (
    "You are the shop's business manager. From the facts and the list of "
    "allowed actions, choose the actions worth taking. You may ONLY use a "
    "capability_key and a target_id that appear in the inputs. Give one "
    "grounded sentence per action. Propose nothing if nothing is worth doing."
)

_INTENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "intents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "capability_key": {"type": "string"},
                    "target_id": {"type": "string"},
                    "params": {"type": "object", "additionalProperties": True},
                    "reason": {"type": "string"},
                },
                "required": ["capability_key", "target_id", "params", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["intents"],
    "additionalProperties": False,
}

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

    def plan(self, facts_json: str, catalog: list[CapabilityDescriptor]) -> PlannerPlan:
        catalog_json = json.dumps([c.model_dump() for c in catalog])
        user_content = f"FACTS:\n{facts_json}\n\nALLOWED CAPABILITIES:\n{catalog_json}"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "planner_intents",
                    "strict": True,
                    "schema": _INTENTS_SCHEMA,
                },
            },
        }
        resp = self._client.post(BASE, json=body)
        resp.raise_for_status()
        payload = resp.json()

        try:
            text = payload["choices"][0]["message"]["content"]
            parsed = json.loads(text)
            intents = [ProposalIntent.model_validate(i) for i in parsed["intents"]]
        except (KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raise PlannerParseError(
                f"could not parse OpenRouter response: {resp.text[:_MAX_ERROR_LEN]!r}"
            ) from exc

        return PlannerPlan(intents=intents, usage=self._build_usage(payload))

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
