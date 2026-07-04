"""Gemini vision adapter. httpx only — no vendor SDK (PRD §13 decision 22).

NOT wired to any default path; live use is triple-gated (flag + env + key)
by the caller per PRD §8.4.
"""

import base64
import json

import httpx
from pydantic import ValidationError

from shopsteward.adapters.vision.interface import VisionResult, VisionUsage, VisionVerdict

BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_VERDICT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "commercial_score": {"type": "INTEGER"},
        "subject": {"type": "STRING"},
        "strongest_room_style": {"type": "STRING"},
        "one_risk": {"type": "STRING"},
        "rationale": {"type": "STRING"},
    },
    "required": [
        "commercial_score",
        "subject",
        "strongest_room_style",
        "one_risk",
        "rationale",
    ],
}


class VisionParseError(RuntimeError):
    """Raised when a Gemini response cannot be parsed into a VisionVerdict."""


class GeminiVisionAdapter:
    def __init__(
        self,
        api_key: str,
        prompt: str,
        pricing: dict[str, dict[str, float]] | None = None,
        timeout: float = 60.0,
    ):
        self._prompt = prompt
        self._pricing = pricing
        self._client = httpx.Client(headers={"x-goog-api-key": api_key}, timeout=timeout)

    def score_commercial(self, jpeg_bytes: bytes, *, model: str) -> VisionResult:
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/jpeg",
                                "data": base64.b64encode(jpeg_bytes).decode("ascii"),
                            }
                        },
                        {"text": self._prompt},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _VERDICT_SCHEMA,
            },
        }
        resp = self._client.post(f"{BASE}/{model}:generateContent", json=body)
        resp.raise_for_status()
        payload = resp.json()

        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            verdict_json = json.loads(text)
            verdict = VisionVerdict.model_validate(verdict_json)
        except (KeyError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raise VisionParseError(f"could not parse Gemini response: {payload!r}") from exc

        usage = self._build_usage(payload, model)
        return VisionResult(verdict=verdict, usage=usage)

    def _build_usage(self, payload: dict, model: str) -> VisionUsage:
        meta = payload.get("usageMetadata", {})
        input_tokens = meta.get("promptTokenCount")
        output_tokens = meta.get("candidatesTokenCount")

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
