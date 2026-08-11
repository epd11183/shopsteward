"""NEEDS YOU / DONE / REFUSED / AUTONOMY Brief sections (M8a spec §8 PR3,
draft §6). Events are appended with explicit `created_at` (bypassing
core.events.append()'s DB-default timestamp, tests/pipeline/ops/helpers.py
precedent) so the 7-day DONE/REFUSED window can be tested against a fixed
anchor date rather than real wall-clock time. Still a plain INSERT, never an
UPDATE/DELETE."""

import json
from datetime import date, timedelta

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.models import ProposedAction, Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import compute_action_id

USER_ID = 1
AS_OF = date(2026, 2, 10)
CAPABILITY = "stub.brief_test"


def _insert(conn, type_: str, payload: dict, day: date) -> None:
    created_at = f"{day.isoformat()}T00:00:00.000000Z"
    conn.execute(
        "INSERT INTO events (user_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        (USER_ID, type_, json.dumps(payload), created_at),
    )
    conn.commit()


def _proposed(target_id: str, cfg_hash: str, *, proposed_day: date, reason: str) -> ProposedAction:
    action_id = compute_action_id(
        CAPABILITY, target_id, "fixed-inputs", cfg_hash, proposed_day.isoformat()
    )
    return ProposedAction(
        action_id=action_id,
        capability=CAPABILITY,
        target_type="stub",
        target_id=target_id,
        tier=Tier.PROPOSE,
        reason=reason,
        inputs_hash="fixed-inputs",
        estimated_cost_usd=0.0,
        undo_available=True,
        expires_at=(proposed_day + timedelta(days=14)).isoformat(),
    )


def _seed_scenario(conn, cfg):
    cfg_hash = ops_config.ops_config_hash(cfg)

    # t1: still open -- must show up in NEEDS YOU.
    p1 = _proposed("t1", cfg_hash, proposed_day=AS_OF, reason="t1 is a dead listing candidate.")
    _insert(conn, "action.proposed", p1.model_dump(), AS_OF)

    # t2: approved + executed 5 days ago -- inside the 7-day DONE window.
    p2 = _proposed(
        "t2", cfg_hash, proposed_day=AS_OF - timedelta(days=5), reason="t2 auto-renew off."
    )
    day2 = AS_OF - timedelta(days=5)
    _insert(conn, "action.proposed", p2.model_dump(), day2)
    _insert(conn, "action.approved", {"action_id": p2.action_id, "by": "operator"}, day2)
    _insert(
        conn,
        "action.executed",
        {
            "action_id": p2.action_id,
            "before": {"should_auto_renew": True},
            "after": {"should_auto_renew": False},
            "cost_usd": 0.0,
            "duration_ms": 1,
        },
        day2,
    )

    # t3: executed 8 days ago -- outside the 7-day DONE window, must be excluded.
    p3 = _proposed(
        "t3", cfg_hash, proposed_day=AS_OF - timedelta(days=8), reason="t3 auto-renew off."
    )
    day3 = AS_OF - timedelta(days=8)
    _insert(conn, "action.proposed", p3.model_dump(), day3)
    _insert(conn, "action.approved", {"action_id": p3.action_id, "by": "operator"}, day3)
    _insert(
        conn,
        "action.executed",
        {
            "action_id": p3.action_id,
            "before": {"should_auto_renew": True},
            "after": {"should_auto_renew": False},
            "cost_usd": 0.0,
            "duration_ms": 1,
        },
        day3,
    )

    # t4: refused by the governor 3 days ago -- inside the 7-day REFUSED window.
    p4 = _proposed(
        "t4", cfg_hash, proposed_day=AS_OF - timedelta(days=3), reason="t4 auto-renew off."
    )
    day4 = AS_OF - timedelta(days=3)
    _insert(conn, "action.proposed", p4.model_dump(), day4)
    _insert(conn, "action.refused", {"action_id": p4.action_id, "reason": "budget"}, day4)

    return p1, p2, p3, p4


def _built(tmp_path, *, autonomy_section: bool = True):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    sections = cfg.brief_sections.model_copy(update={"autonomy": autonomy_section})
    cfg = cfg.model_copy(update={"brief_sections": sections})
    ids = _seed_scenario(conn, cfg)
    return conn, cfg, ids


def test_generate_brief_populates_needs_you_done_and_refused(tmp_path):
    conn, cfg, (p1, p2, p3, p4) = _built(tmp_path)

    brief = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)

    assert [p.action_id for p in brief.needs_you] == [p1.action_id]
    assert brief.needs_you[0].capability == CAPABILITY
    assert brief.needs_you[0].expires_at == p1.expires_at

    assert [a.action_id for a in brief.done_recent] == [p2.action_id]  # p3 (8d old) excluded
    assert brief.done_recent[0].undo_available is True

    assert [r.target_id for r in brief.refused_recent] == [p4.target_id]
    assert brief.refused_recent[0].reason == "budget"


def test_generate_brief_populates_autonomy_ladder(tmp_path):
    conn, cfg, _ids = _built(tmp_path)

    brief = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)

    assert brief.autonomy is not None
    assert brief.autonomy.enabled == cfg.autonomy.enabled
    assert brief.autonomy.monthly_spend_cap_usd == cfg.autonomy.monthly_spend_cap_usd
    ladder_keys = {row.capability for row in brief.autonomy.ladder}
    assert CAPABILITY in ladder_keys
    row = next(r for r in brief.autonomy.ladder if r.capability == CAPABILITY)
    assert row.approvals == 2  # t2 and t3 were both operator-approved


def test_render_text_shows_action_ids_in_needs_you_and_done(tmp_path):
    conn, cfg, (p1, p2, p3, p4) = _built(tmp_path)
    brief = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)

    text = render_text(brief)

    assert "NEEDS YOU (1)" in text
    assert p1.action_id in text
    assert "DONE (1)" in text
    assert p2.action_id in text
    assert f"ops undo {p2.action_id}" in text
    assert p3.action_id not in text  # excluded by the 7-day window
    assert "REFUSED (1)" in text
    assert "budget" in text


def test_render_text_has_a_spend_of_cap_autonomy_line(tmp_path):
    conn, cfg, _ids = _built(tmp_path)
    brief = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)

    text = render_text(brief)

    assert "AUTONOMY" in text
    assert f"of ${cfg.autonomy.monthly_spend_cap_usd:,.2f} cap" in text


def test_autonomy_section_disabled_by_config_toggle_is_empty_and_omitted_from_text(tmp_path):
    conn, cfg, _ids = _built(tmp_path, autonomy_section=False)

    brief = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)

    assert brief.needs_you == []
    assert brief.done_recent == []
    assert brief.refused_recent == []
    assert brief.autonomy is None

    text = render_text(brief)
    assert "NEEDS YOU" not in text
    assert "DONE (" not in text
    assert "REFUSED" not in text
    assert "AUTONOMY" not in text
    assert "THE SHOP" in text  # the rest of the brief is unaffected


def test_existing_slice1_brief_fields_still_default_empty_without_the_new_seeding(tmp_path):
    """A caller that never seeds any action.* events (slice-1 shape) must
    get an unchanged, valid Brief -- the new fields default empty."""
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)

    brief = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)

    assert brief.needs_you == []
    assert brief.done_recent == []
    assert brief.refused_recent == []
    assert brief.autonomy is not None  # section toggle defaults True
    assert brief.autonomy.ladder == []
