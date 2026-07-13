"""OpenRouter vision adapter. httpx only — no vendor SDK (PRD §13 decision 22,
decision 36). NOT wired to any default path; live use is triple-gated (flag +
env + key) by the caller per PRD §8.4.

Runtime AI calls route through OpenRouter (PRD §13 decision 36) so spend is
tracked per-project and models are swappable via configuration with no code
change. The vision models stay in the Gemini family; OpenRouter adds no
markup on them.
"""

import base64
import json

import httpx
from pydantic import ValidationError

from shopsteward.adapters.vision.interface import (
    VisionParseError,
    VisionResult,
    VisionUsage,
    VisionVerdict,
)

BASE = "https://openrouter.ai/api/v1/chat/completions"

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "commercial_score": {"type": "integer"},
        "subject": {"type": "string"},
        "strongest_room_style": {"type": "string"},
        "one_risk": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": [
        "commercial_score",
        "subject",
        "strongest_room_style",
        "one_risk",
        "rationale",
    ],
    "additionalProperties": False,
}

_MAX_ERROR_LEN = 500


class OpenRouterVisionAdapter:
    def __init__(
        self,
        api_key: str,
        prompt: str,
        pricing: dict[str, dict[str, float]] | None = None,
        timeout: float = 60.0,
    ):
        self._prompt = prompt
        self._pricing = pricing
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/epd11183/shopsteward",
                "X-Title": "ShopSteward",
            },
            timeout=timeout,
        )

    def score_commercial(self, jpeg_bytes: bytes, *, model: str) -> VisionResult:
        body = {
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    "data:image/jpeg;base64,"
                                    f"{base64.b64encode(jpeg_bytes).decode('ascii')}"
                                )
                            },
                        },
                        {"type": "text", "text": self._prompt},
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vision_verdict",
                    "strict": True,
                    "schema": _VERDICT_SCHEMA,
                },
            },
        }
        resp = self._client.post(BASE, json=body)
        resp.raise_for_status()
        payload = resp.json()

        try:
            text = payload["choices"][0]["message"]["content"]
            verdict_json = json.loads(text)
            verdict = VisionVerdict.model_validate(verdict_json)
        except (KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raise VisionParseError(
                f"could not parse OpenRouter response: {payload!r:.{_MAX_ERROR_LEN}}"
            ) from exc

        usage = self._build_usage(payload, model)
        return VisionResult(verdict=verdict, usage=usage)

    def _build_usage(self, payload: dict, model: str) -> VisionUsage:
        meta = payload.get("usage", {})
        input_tokens = meta.get("prompt_tokens")
        output_tokens = meta.get("completion_tokens")

        est_cost_usd = None
        have_tokens = input_tokens is not None and output_tokens is not None
        if self._pricing and model in self._pricing and have_tokens:
            rates = self._pricing[model]
            est_cost_usd = (input_tokens / 1e6) * rates["in"] + (output_tokens / 1e6) * rates["out"]

        return VisionUsage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=est_cost_usd,
        )
