"""Editing-local live-look gate. The editing module cannot import
pipeline.live_gate (import-linter), so it mirrors that convention here: live
Claude-via-OpenRouter look generation requires an explicit flag + the API key."""

import os


def live_look_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_LOOK=1 and OPENROUTER_API_KEY are both set."""
    return os.environ.get("SHOPSTEWARD_LIVE_LOOK") == "1" and bool(
        os.environ.get("OPENROUTER_API_KEY")
    )


def live_look_error() -> str:
    return (
        "Live look generation is gated on operator approval: set "
        "SHOPSTEWARD_LIVE_LOOK=1 and OPENROUTER_API_KEY in the environment, "
        "then re-run with --live-look."
    )
