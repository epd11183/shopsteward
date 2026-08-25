"""Parse the timestamp string formats that actually appear in this chassis's
event log (review finding E2): `events.created_at` is stamped by
`core/db.py`'s own SQLite default (`strftime('%Y-%m-%dT%H:%M:%fZ','now')` --
a literal `Z` suffix), application code that writes its own timestamp into a
payload uses `datetime.now(UTC).isoformat()` (a `+00:00` suffix), and some
fields (`ProposedAction.expires_at`) are a bare ISO date with no time
component at all. These three strings do NOT sort consistently against each
other lexicographically at second-level ties (`'Z' > '+00:00'` as plain
characters, but neither is a fixed-width, fixed-offset representation) --
`parse_ts()` turns any of them into a real, UTC-aware `datetime` so callers
compare values, never strings."""

from datetime import UTC, date, datetime


def parse_ts(value: str) -> datetime:
    """Accepts `'YYYY-MM-DDTHH:MM:SS.ffffffZ'` (core/db.py's SQLite
    default, with or without the fractional-second component),
    `datetime.isoformat()`'s `'+HH:MM'`-suffixed form, or a bare
    `'YYYY-MM-DD'` date (treated as midnight UTC). Always returns a
    UTC-aware datetime, safe to compare directly against another value this
    function returns."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # A bare date (e.g. ProposedAction.expires_at) has no fromisoformat
        # time component to parse -- fall back to date.fromisoformat() and
        # treat it as midnight UTC.
        dt = datetime.combine(date.fromisoformat(v), datetime.min.time())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
