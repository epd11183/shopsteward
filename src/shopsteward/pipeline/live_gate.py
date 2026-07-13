"""Live-vision environment gate (PRD §8.4, amended by §13 decision 36), shared
by the CLI and the API so the refusal message and the flag+env+key check only
live in one place. Provider-aware: which API key is required depends on
`tuning_profile.vision.provider` ("openrouter" default, or "gemini" fallback).
"""

import os

_PROVIDER_ENV_KEYS = {
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _key_env_var(provider: str) -> str:
    try:
        return _PROVIDER_ENV_KEYS[provider]
    except KeyError as exc:
        raise ValueError(f"unknown vision provider {provider!r}") from exc


def live_vision_open(provider: str) -> bool:
    """True iff SHOPSTEWARD_LIVE_VISION=1 and the provider's API key env var
    are both set."""
    key_env = _key_env_var(provider)
    return os.environ.get("SHOPSTEWARD_LIVE_VISION") == "1" and bool(os.environ.get(key_env))


def live_vision_error(provider: str) -> str:
    """Refusal message naming the correct env var for `provider`."""
    key_env = _key_env_var(provider)
    return (
        "Live vision scoring is gated on operator approval (PRD §8.4): set "
        f"SHOPSTEWARD_LIVE_VISION=1 and {key_env} in the environment, "
        "then re-run with --live-vision."
    )
