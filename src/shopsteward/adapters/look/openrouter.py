"""OpenRouter look adapter. httpx only — no vendor SDK (adapters.copy.openrouter
precedent). NOT wired to any default path; live use is gated (flag + env + key)
by the caller. Text-only: only the look description is sent, never a photograph."""

import json

import httpx
from pydantic import ValidationError

from shopsteward.adapters.look.interface import (
    LookParseError,
    LookProfile,
    LookResult,
    LookUsage,
)

BASE = "https://openrouter.ai/api/v1/chat/completions"

_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "contrast": {"type": "integer", "minimum": -100, "maximum": 100},
        "tone_curve": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        },
        "hsl": {"type": "object", "additionalProperties": {"type": "integer"}},
        "split_toning": {"type": "object", "additionalProperties": {"type": "integer"}},
        "vibrance": {"type": "integer", "minimum": -100, "maximum": 100},
        "saturation": {"type": "integer", "minimum": -100, "maximum": 100},
    },
    "required": ["contrast", "tone_curve", "hsl", "split_toning", "vibrance", "saturation"],
    "additionalProperties": False,
}

_MAX_ERROR_LEN = 500


class OpenRouterLookAdapter:
    def __init__(
        self,
        api_key: str,
        prompt_template: str,
        pricing: dict[str, dict[str, float]] | None = None,
        temperature: float = 0.7,
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

    def generate_look(self, description: str, *, model: str) -> LookResult:
        prompt = self._prompt_template.format(description=description)
        body = {
            "model": model,
            "temperature": self._temperature,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "look_profile", "strict": True, "schema": _PROFILE_SCHEMA},
            },
        }
        resp = self._client.post(BASE, json=body)
        resp.raise_for_status()
        payload = resp.json()

        try:
            text = payload["choices"][0]["message"]["content"]
            data = json.loads(text)
            data["name"] = description
            data["description"] = description
            profile = LookProfile.model_validate(data)
        except (KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raise LookParseError(
                f"could not parse OpenRouter response: {payload!r:.{_MAX_ERROR_LEN}}"
            ) from exc

        return LookResult(profile=profile, usage=self._build_usage(payload, model))

    def _build_usage(self, payload: dict, model: str) -> LookUsage:
        meta = payload.get("usage", {})
        input_tokens = meta.get("prompt_tokens")
        output_tokens = meta.get("completion_tokens")
        est_cost_usd = None
        have = input_tokens is not None and output_tokens is not None
        if self._pricing and model in self._pricing and have:
            rates = self._pricing[model]
            est_cost_usd = (input_tokens / 1e6) * rates["in"] + (output_tokens / 1e6) * rates["out"]
        return LookUsage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=est_cost_usd,
        )
