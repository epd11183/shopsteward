"""Live-vision environment gate (PRD §8.4), shared by the CLI and the API so
the refusal message and the flag+env+key check only live in one place."""

import os

LIVE_VISION_ERROR = (
    "Live vision scoring is gated on operator approval (PRD §8.4): set "
    "SHOPSTEWARD_LIVE_VISION=1 and GEMINI_API_KEY in the environment, "
    "then re-run with --live-vision."
)


def live_vision_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_VISION=1 and GEMINI_API_KEY are both set."""
    return os.environ.get("SHOPSTEWARD_LIVE_VISION") == "1" and bool(
        os.environ.get("GEMINI_API_KEY")
    )
