"""Fix 2 (2026-08-24 follow-up): `analytics.pin_experiment_readout()` must
not grow unbounded -- caps to the most recent `_PIN_EXPERIMENTS_MAX_ROWS`
rows. The cap must not accidentally drop a row that is old in absolute
terms but was, at the time, still measurable/kept -- only rows beyond the
cap (the OLDEST ones once the list overflows) are ever dropped."""

from datetime import date, timedelta

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import analytics
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.analytics import _PIN_EXPERIMENTS_MAX_ROWS
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
LISTING_ID = 999
AS_OF = date(2026, 6, 1)
TOTAL_ROWS = _PIN_EXPERIMENTS_MAX_ROWS + 50


def _seed_daily_history(conn) -> None:
    # Steady +1 view/day for 250 days back from AS_OF -- covers every
    # before/after measurement window used below, for every drafted_at.
    for offset in range(250, -1, -1):
        seed_listing_observed_on(
            conn,
            listing_id=LISTING_ID,
            title="Cap Test Print",
            day=AS_OF - timedelta(days=offset),
            views=1000 - offset,
        )


def _seed_pin_experiment_rows(conn, n: int) -> list[tuple[str, date]]:
    """Directly INSERTs into proj_pin_experiments (bypassing the event-fold
    -- rebuild_ops() must NOT be called again after this, or it would wipe
    these rows). `n` drafted_at dates spread from oldest (`n - 1` days
    before AS_OF) to newest (AS_OF itself, i.e. "drafted today" -- too early
    to ever be measurable)."""
    rows = []
    for i in range(n):
        drafted_at = AS_OF - timedelta(days=n - 1 - i)
        action_id = f"act-{i:04d}"
        conn.execute(
            "INSERT INTO proj_pin_experiments VALUES (?,?,?,?,?)",
            (USER_ID, LISTING_ID, action_id, drafted_at.isoformat(), None),
        )
        rows.append((action_id, drafted_at))
    conn.commit()
    return rows


def test_list_is_genuinely_bounded():
    conn = connect(":memory:")
    migrate(conn)
    _seed_daily_history(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)  # builds proj_listing_daily from the seeded events
    rows = _seed_pin_experiment_rows(conn, TOTAL_ROWS)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    results = analytics.pin_experiment_readout(conn, USER_ID, cfg, as_of=AS_OF)

    assert len(results) == _PIN_EXPERIMENTS_MAX_ROWS
    # Only the OLDEST rows (beyond the cap) are dropped.
    kept_ids = {r.action_id for r in results}
    oldest_action_id = rows[0][0]
    newest_action_id = rows[-1][0]
    assert oldest_action_id not in kept_ids
    assert newest_action_id in kept_ids


def test_a_not_yet_measurable_row_is_never_dropped_by_the_cap():
    """The newest row (drafted_at == AS_OF) is always within the kept
    window (it's the very last, most-recent one), so its 'too early to
    measure' state must survive the cap intact."""
    conn = connect(":memory:")
    migrate(conn)
    _seed_daily_history(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    _seed_pin_experiment_rows(conn, TOTAL_ROWS)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    results = analytics.pin_experiment_readout(conn, USER_ID, cfg, as_of=AS_OF)

    newest = max(results, key=lambda r: r.drafted_at)
    assert newest.drafted_at == AS_OF.isoformat()
    assert newest.observed_views_per_day is None  # too early -- not zero


def test_an_older_but_still_measurable_row_within_the_cap_is_not_dropped():
    """A row well inside the cap window but old enough for its full
    before/after measurement to have completed must still show a real
    delta, not be silently excluded."""
    conn = connect(":memory:")
    migrate(conn)
    _seed_daily_history(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    rows = _seed_pin_experiment_rows(conn, TOTAL_ROWS)

    cfg = ops_config.get_ops_config(conn, USER_ID)
    results = analytics.pin_experiment_readout(conn, USER_ID, cfg, as_of=AS_OF)
    by_id = {r.action_id: r for r in results}

    # The oldest row still inside the cap (index 50, i.e. rows[TOTAL_ROWS -
    # _PIN_EXPERIMENTS_MAX_ROWS]) is >7 days before AS_OF -- fully
    # measurable, and must be present with a real (non-None) reading.
    oldest_kept_action_id, oldest_kept_drafted = rows[TOTAL_ROWS - _PIN_EXPERIMENTS_MAX_ROWS]
    assert oldest_kept_action_id in by_id
    kept = by_id[oldest_kept_action_id]
    assert kept.baseline_views_per_day is not None
    assert kept.observed_views_per_day is not None
