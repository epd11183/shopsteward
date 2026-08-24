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
    PlannerLimits,
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

# Etsy's real tag/title field limits (not tuning knobs -- kept as local
# constants here, mirroring pipeline/ops/capabilities/seo_edit.py's own
# _MAX_TITLE_LEN/_MAX_TAGS/_MAX_TAG_LEN, rather than imported: no adapter may
# import `pipeline` (see adapters/planner/interface.py's module docstring)).
_MAX_TITLE_LEN = 140
_MAX_TAGS = 13
_MAX_TAG_LEN = 20


def _plan_system_prompt(limits: PlannerLimits) -> str:
    """Built per-call from `limits` (never a fixed module constant) so the
    numeric bounds quoted to the model can never drift out of sync with what
    `_validate_params()`/`_is_in_bounds_price()`/`_valid_caption()` actually
    enforce server-side if an operator changes `config/defaults/ops.json`.

    Known gap (`_build_facts_json`, pipeline/ops/planner.py): the facts JSON
    surfaces a listing's current `title` (dead_listings/viewed_not_sold/
    trending/top_sellers all carry it) but never its current `price_usd` or
    `tags` -- the model can propose a genuinely different title because it
    can see the old one, but it is guessing blind on price/tags. The
    re-validation below (min/max/diff) still happens server-side either way
    (`_validate_params`/`_is_in_bounds_price`), so an out-of-bounds or
    accidentally-unchanged guess is dropped, never silently accepted -- this
    is a prompt-quality gap, not a safety one."""
    return (
        "You are the shop's business manager. From the facts and the list of "
        "allowed actions, choose the actions worth taking. You may ONLY use a "
        "capability_key and a target_id that appear in the inputs. Give one "
        "grounded sentence per action. Propose nothing if nothing is worth "
        "doing.\n\n"
        "For capabilities that need generated content, `params` MUST contain "
        "the following real fields (any other proposal is silently dropped):\n"
        '- listing.seo_edit: optionally "title" (string, 1-140 chars), "tags" '
        f"(list of 1-{_MAX_TAGS} strings, each 1-{_MAX_TAG_LEN} chars -- "
        'Etsy\'s real tag length limit), and/or "description" (string, '
        "1-5000 chars). At least one of title/tags/description must actually "
        "differ from the listing's current value (its current title is in "
        "the facts; its current tags/description are not, so pick values you "
        "believe are new) or the proposal is a no-op and is dropped. A "
        "description change is only ever kept when the listing already has a "
        "recorded description to diff/restore against -- propose one anyway "
        "if you believe it's an improvement, it will simply be dropped "
        "server-side if there's no baseline.\n"
        '- listing.reprice: "price_usd" (a real, finite number) that is '
        f">= {limits.reprice_min_price_usd}, within +/-"
        f"{limits.reprice_max_pct_change * 100:.0f}% of the listing's current "
        "price, and different from it (the current price is not in the "
        "facts -- propose a value you believe satisfies these bounds; an "
        "out-of-bounds or unchanged guess is dropped, never adjusted).\n"
        '- social.caption_draft: "caption" (a non-empty string, at most '
        f"{limits.caption_max_len} characters).\n"
        "listing.autorenew_off, listing.autorenew_on, listing.deactivate, and "
        "listing.renew take no params -- do not invent any for them.\n"
        "(listing.seo_edit's eligible targets are already either active "
        f"listings with at least {limits.seo_edit_min_lifetime_views} "
        "lifetime views and no recent sale, expired listings with real "
        "historical sales, or active listings with (near-)zero tags "
        "(search-invisible regardless of views) -- you never need to check "
        "any of these conditions yourself, it's given for context.)"
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

    def plan(
        self, facts_json: str, catalog: list[CapabilityDescriptor], limits: PlannerLimits
    ) -> PlannerPlan:
        catalog_json = json.dumps([c.model_dump() for c in catalog])
        user_content = f"FACTS:\n{facts_json}\n\nALLOWED CAPABILITIES:\n{catalog_json}"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _plan_system_prompt(limits)},
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
