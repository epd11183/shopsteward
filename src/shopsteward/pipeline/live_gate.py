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


def live_copy_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_COPY=1 and OPENROUTER_API_KEY are both set
    (M5a copy is OpenRouter-only, PRD §13 decision 38)."""
    return os.environ.get("SHOPSTEWARD_LIVE_COPY") == "1" and bool(
        os.environ.get("OPENROUTER_API_KEY")
    )


def live_copy_error() -> str:
    return (
        "Live listing-copy generation is gated on operator approval (PRD §8.4): set "
        "SHOPSTEWARD_LIVE_COPY=1 and OPENROUTER_API_KEY in the environment, "
        "then re-run with --live-copy."
    )


def live_etsy_read_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_ETSY_READ=1, ETSY_API_KEY is set, and Etsy
    tokens are on disk with the listings_r scope (read analogue of
    live_etsy_write_open, PRD §8.4 -- M1 live sync). Does not check
    shop_id -- that mirrors live_etsy_write_open's contract and is validated
    at adapter-construction time instead."""
    if os.environ.get("SHOPSTEWARD_LIVE_ETSY_READ") != "1":
        return False
    if not os.environ.get("ETSY_API_KEY"):
        return False

    from shopsteward.adapters.etsy.auth import EtsyTokenStore

    tokens = EtsyTokenStore().load()
    return tokens is not None and "listings_r" in tokens.scopes


def live_etsy_read_error() -> str:
    return (
        "Live Etsy read sync is gated on operator approval (PRD §8.4): set "
        "SHOPSTEWARD_LIVE_ETSY_READ=1 and ETSY_API_KEY, run `shopsteward etsy "
        "auth` with the listings_r scope, then re-run with --live."
    )


def live_etsy_write_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_ETSY_WRITE=1, ETSY_API_KEY is set, and Etsy
    tokens are on disk with the listings_w scope (PRD §13 decision 41). The
    key check lives HERE so the CLI refuses up front instead of crashing
    mid-build after copy/price events were already emitted."""
    if os.environ.get("SHOPSTEWARD_LIVE_ETSY_WRITE") != "1":
        return False
    if not os.environ.get("ETSY_API_KEY"):
        return False

    from shopsteward.adapters.etsy.auth import EtsyTokenStore

    tokens = EtsyTokenStore().load()
    return tokens is not None and "listings_w" in tokens.scopes


def live_etsy_write_error() -> str:
    return (
        "Live Etsy draft push is gated on operator approval (PRD §8.4): set "
        "SHOPSTEWARD_LIVE_ETSY_WRITE=1 and ETSY_API_KEY, run `shopsteward etsy "
        "auth` with the listings_w scope, then re-run with --live-etsy-write."
    )


_R2_ENV_VARS = (
    "CLOUDFLARE_R2_KEY",
    "CLOUDFLARE_R2_SECRET",
    "CLOUDFLARE_R2_ENDPOINT",
    "CLOUDFLARE_R2_BUCKET",
)


def live_printfile_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_PRINTFILE=1 and every Cloudflare R2 object
    credential env var is set (design §9, §17 Q1/Q1a). Deliberately never
    checks CLOUDFLARE_R2_TOKEN: that is a Cloudflare account-management
    credential, not an S3 object credential, and the live adapter has no
    code path that accepts it as a substitute."""
    if os.environ.get("SHOPSTEWARD_LIVE_PRINTFILE") != "1":
        return False
    return all(os.environ.get(var) for var in _R2_ENV_VARS)


def live_printfile_error() -> str:
    return (
        "Live print-file hosting is gated on operator approval (PRD §8.4): set "
        "SHOPSTEWARD_LIVE_PRINTFILE=1 and " + ", ".join(_R2_ENV_VARS) + " in the "
        "environment, then re-run with --live-printfile."
    )


def live_gelato_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_GELATO=1, GELATO_API_KEY, and GELATO_STORE_ID
    are all set. GELATO_STORE_ID is required here (not at the model) so the
    offline/fake path stays usable with no Gelato env, while a LIVE run refuses
    up front rather than posting against a placeholder store_id."""
    return (
        os.environ.get("SHOPSTEWARD_LIVE_GELATO") == "1"
        and bool(os.environ.get("GELATO_API_KEY"))
        and bool(os.environ.get("GELATO_STORE_ID"))
    )


def live_gelato_error() -> str:
    return (
        "Live Gelato product creation is gated on operator approval: set "
        "SHOPSTEWARD_LIVE_GELATO=1, GELATO_API_KEY, and GELATO_STORE_ID, fill "
        "real Gelato IDs in pod.json, then re-run with --live-gelato."
    )
