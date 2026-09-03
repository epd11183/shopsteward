"""OpenRouter copy adapter. httpx only — no vendor SDK (PRD §13 decisions 22,
36, 38). NOT wired to any default path; live use is triple-gated (flag + env
+ key) by the caller, mirroring OpenRouterVisionAdapter.

Text-only: unlike the vision adapter, no image is ever sent -- the copy call
only ever sees the house style guide + vision-verdict signals (subject, room
style, risk) already extracted by the M3 vision scorer, never the photograph
itself.
"""

import json

import httpx
from pydantic import ValidationError

from shopsteward.adapters.copy.interface import (
    CopyInputs,
    CopyParseError,
    CopyResult,
    CopyUsage,
    CopyVerdict,
)

BASE = "https://openrouter.ai/api/v1/chat/completions"

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 140},
        "tags": {
            "type": "array",
            "maxItems": 13,
            "items": {"type": "string", "maxLength": 20},
        },
        "description": {"type": "string"},
        "materials": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
    },
    "required": ["title", "tags", "description", "materials"],
    "additionalProperties": False,
}

_MAX_ERROR_LEN = 500


class OpenRouterCopyAdapter:
    def __init__(
        self,
        api_key: str,
        prompt_template: str,
        pricing: dict[str, dict[str, float]] | None = None,
        temperature: float = 0.4,
        timeout: float = 60.0,
    ):
        self._prompt_template = prompt_template
        self._pricing = pricing
        self._temperature = temperature
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/epd11183/shopsteward",
                "X-Title": "ShopSteward",
            },
            timeout=timeout,
        )

    def generate_copy(self, inputs: CopyInputs, *, model: str) -> CopyResult:
        prompt = self._render_prompt(inputs)
        body = {
            "model": model,
            "temperature": self._temperature,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "listing_copy",
                    "strict": True,
                    "schema": _VERDICT_SCHEMA,
                },
            },
        }
        resp = self._client.post(BASE, json=body)
        resp.raise_for_status()

        # payload = resp.json() used to sit outside this try, so a truncated
        # HTTP body (not just truncated model content) raised a bare
        # json.JSONDecodeError instead of CopyParseError -- the caller's
        # narrow `except CopyParseError` (copy.py) wouldn't catch that, and
        # it would crash the whole pipeline the same way the original bug
        # did. Bringing it inside means ANY malformed-response shape --
        # truncated body or truncated content -- surfaces the same way.
        payload: object = None
        try:
            payload = resp.json()
            text = payload["choices"][0]["message"]["content"]
            verdict_json = json.loads(text)
            verdict = CopyVerdict.model_validate(verdict_json)
        except (KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raw = payload if payload is not None else resp.text
            raise CopyParseError(
                f"could not parse OpenRouter response: {raw!r:.{_MAX_ERROR_LEN}}"
            ) from exc

        usage = self._build_usage(payload, model)
        return CopyResult(verdict=verdict, usage=usage)

    def _render_prompt(self, inputs: CopyInputs) -> str:
        return self._prompt_template.format(
            house_style=inputs.house_style,
            subject=inputs.subject or "unspecified",
            strongest_room_style=inputs.strongest_room_style or "unspecified",
            one_risk=inputs.one_risk or "unspecified",
            rationale=inputs.rationale or "unspecified",
            orientation=inputs.orientation,
            format=inputs.format,
            sizes=", ".join(inputs.sizes),
            formats=", ".join(inputs.formats),
        )

    def _build_usage(self, payload: dict, model: str) -> CopyUsage:
        meta = payload.get("usage", {})
        input_tokens = meta.get("prompt_tokens")
        output_tokens = meta.get("completion_tokens")

        est_cost_usd = None
        have_tokens = input_tokens is not None and output_tokens is not None
        if self._pricing and model in self._pricing and have_tokens:
            rates = self._pricing[model]
            est_cost_usd = (input_tokens / 1e6) * rates["in"] + (output_tokens / 1e6) * rates["out"]

        return CopyUsage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=est_cost_usd,
        )
