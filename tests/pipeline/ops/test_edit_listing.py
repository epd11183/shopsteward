"""2026-11-22 operator decision: listing edits need no approval.

Covers (all against `FakeEtsyWriteAdapter`, never live):
- the AUTO floor / ceilings of the real listing capabilities (gapfill and
  catalog_expand stay PROPOSE: no undo);
- `listing.seo_edit` working on plain text when Etsy returns HTML-escaped text;
- `ops edit-listing` (`cli.run_direct_edit` + the typer verb).
"""

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

import shopsteward.pipeline.listings.push as push_mod
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.planner.interface import ProposalIntent
from shopsteward.adapters.vision.fake import FixtureCopyChecker
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.seo_edit import (
    DIRECT_PARAM,
    ListingSeoEdit,
    _eligible,
    _Target,
)
from shopsteward.pipeline.ops.cli import EditListingError, run_direct_edit
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, register
from shopsteward.pipeline.ops.runner import run, undo_action
from tests.pipeline.ops.helpers import seed_listing_observed_on

USER_ID = 1
TODAY = datetime.now(UTC).date()
LID = 901
runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


def _seed(conn, *, title="Loon at Dusk", tags=("loon",), description="Plain baseline.", views=100):
    for days_ago in (200, 1):
        seed_listing_observed_on(
            conn,
            listing_id=LID,
            title=title,
            day=TODAY - timedelta(days=days_ago),
            views=views,
            tags=list(tags),
            description=description,
        )
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _fake(title="Loon at Dusk", tags=("loon",)) -> FakeEtsyWriteAdapter:
    fake = FakeEtsyWriteAdapter()
    fake.seed_listing(LID, state="active", title=title, tags=list(tags))
    return fake


def _edit(conn, fake, **kw):
    cap = ListingSeoEdit(fake)
    register(cap)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    kw.setdefault("live_autonomy", True)
    return run_direct_edit(conn, USER_ID, cfg, cap, LID, **kw), cap


def _updates(fake):
    return [c[1]["fields"] for c in fake.calls if c[0] == "update_listing"]


# --- tiers ------------------------------------------------------------------


def test_real_listing_capabilities_floor_auto_with_undo_and_gapfill_stays_propose():
    from shopsteward.pipeline.listings.pod.factory import build_print_file_host
    from shopsteward.pipeline.ops.capabilities.autorenew import ListingAutorenewOff
    from shopsteward.pipeline.ops.capabilities.autorenew_on import ListingAutorenewOn
    from shopsteward.pipeline.ops.capabilities.catalog_expand import ListingCatalogExpand
    from shopsteward.pipeline.ops.capabilities.deactivate import ListingDeactivate
    from shopsteward.pipeline.ops.capabilities.gapfill import ListingGapfillReprint
    from shopsteward.pipeline.ops.capabilities.renew import ListingRenew
    from shopsteward.pipeline.ops.capabilities.reprice import ListingReprice
    from shopsteward.pipeline.ops.capabilities.size_claims_fix import ListingSizeClaimsFix
    from shopsteward.pipeline.ops.tiers import effective_tier

    fake = FakeEtsyWriteAdapter()
    floor = ops_config.load_ops_config().autonomy.tier_floor
    auto_caps = [
        ListingSeoEdit(fake),
        ListingSizeClaimsFix(fake),
        ListingReprice(fake),
        ListingDeactivate(fake),
        ListingRenew(fake),
        ListingAutorenewOff(fake),
    ]
    for cap in auto_caps:
        register(cap)  # registry invariant 1: AUTO needs a callable undo
        assert cap.max_tier == Tier.AUTO and callable(cap.undo)
        assert effective_tier(cap, None, floor) == Tier.AUTO

    # may run at AUTO, but starts at PROPOSE: recurring fees the cap can't see
    on = ListingAutorenewOn(fake)
    assert on.max_tier == Tier.AUTO and effective_tier(on, None, floor) == Tier.PROPOSE

    # no undo -> must stay operator-approved (registry invariant 1 / paid POD SKU)
    gapfill = ListingGapfillReprint(build_print_file_host(live=False))
    assert gapfill.max_tier == Tier.PROPOSE and gapfill.undo is None
    assert effective_tier(gapfill, None, floor) == Tier.PROPOSE

    # catalog_expand: AUTO since 2026-11-23, behind the read-back gate; undo deactivates
    expand = ListingCatalogExpand(
        fake, live_copy=False, checker=FixtureCopyChecker(), live_check=False
    )
    register(expand)
    assert expand.max_tier == Tier.AUTO and callable(expand.undo)
    assert effective_tier(expand, None, floor) == Tier.AUTO


# --- HTML-escaped baselines -> plain text ------------------------------------


def test_target_normalizes_escaped_title_tags_and_description():
    t = _Target(
        listing_id=1,
        current_title="Bob&#39;s &amp; Co",
        current_tags=["it&#39;s", "plain"],
        current_description="a &lt;b&gt; &amp; c",
        lifetime_views=0,
        reason="r",
    )
    assert t.current_title == "Bob's & Co"
    assert t.current_tags == ["it's", "plain"]
    assert t.current_description == "a <b> & c"


def test_escaped_baseline_plain_proposal_sends_plain_update_and_undo_restores_plain(conn):
    _seed(conn, title="Bob&#39;s Loon", description="It&#39;s a loon &amp; an owl.")
    fake = _fake(title="Bob&#39;s Loon")
    cap = ListingSeoEdit(fake)
    cfg = ops_config.get_ops_config(conn, USER_ID)

    new_desc = "It's a loon & an owl, framed."
    intent = ProposalIntent(
        capability_key="listing.seo_edit",
        target_id=str(LID),
        params={"title": "Bob's Loon Print", "description": new_desc},
        reason="x",
    )
    action = cap.materialize(conn, USER_ID, cfg, intent)
    assert action is not None
    register(cap)
    report = run(conn, USER_ID, cfg, [cap], today=TODAY, proposals=[action], live_autonomy=True)
    assert report.executed == 1

    [sent] = _updates(fake)
    assert sent == {"title": "Bob's Loon Print", "description": new_desc}  # plain, no entities
    executed = read_all(conn, "action.executed")[0].payload
    assert executed["before"] == {
        "title": "Bob's Loon",
        "description": "It's a loon & an owl.",
    }

    undo_action(conn, USER_ID, action.action_id, [cap], live_autonomy=True)
    assert _updates(fake)[1] == {
        "title": "Bob's Loon",
        "description": "It's a loon & an owl.",
    }
    assert "&#" not in str(_updates(fake)) and "&amp;" not in str(_updates(fake))


def test_plain_proposal_equal_to_unescaped_baseline_is_a_noop(conn):
    _seed(conn, description="It&#39;s a loon.")
    cap = ListingSeoEdit(_fake())
    cfg = ops_config.get_ops_config(conn, USER_ID)
    intent = ProposalIntent(
        capability_key="listing.seo_edit",
        target_id=str(LID),
        params={"description": "It's a loon."},
        reason="x",
    )
    assert cap.materialize(conn, USER_ID, cfg, intent) is None


def test_undo_unescapes_a_before_recorded_by_a_pre_fix_execution(conn):
    _seed(conn)
    fake = _fake()
    cap = ListingSeoEdit(fake)
    register(cap)
    from shopsteward.pipeline.ops.models import ProposedAction

    action = ProposedAction(
        action_id="legacy",
        capability="listing.seo_edit",
        target_type="listing",
        target_id=str(LID),
        tier=Tier.AUTO,
        reason="r",
        inputs_hash="h",
        undo_available=True,
        expires_at="2999-01-01",
        params={},
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="action.executed",
            payload={
                "action_id": "legacy",
                "before": {"title": "Bob&#39;s", "tags": ["it&#39;s"], "description": "a &amp; b"},
                "after": {},
                "cost_usd": 0.0,
                "duration_ms": 0,
            },
        ),
    )
    cap.undo(conn, USER_ID, action)
    assert _updates(fake) == [{"title": "Bob's", "tags": ["it's"], "description": "a & b"}]


# --- ops edit-listing ---------------------------------------------------------


def test_direct_description_only_runs_through_the_runner_at_auto(conn):
    _seed(conn)
    fake = _fake()
    (action_id, changes), _ = _edit(conn, fake, description="Brand new description.")

    assert changes == {"description": ("Plain baseline.", "Brand new description.")}
    assert _updates(fake) == [{"description": "Brand new description."}]
    proposed = read_all(conn, "action.proposed")[0].payload
    assert proposed["action_id"] == action_id
    assert proposed["tier"] == int(Tier.AUTO) and proposed["params"][DIRECT_PARAM] is True
    approved = read_all(conn, "action.approved")[0].payload
    assert approved["by"] == "tier:T0"  # no operator approval recorded
    assert read_all(conn, "action.executed")[0].payload["before"] == {
        "description": "Plain baseline."
    }


def test_direct_title_and_tags(conn):
    _seed(conn)
    fake = _fake()
    (_, changes), _ = _edit(
        conn, fake, title="Loon Wall Art", tags=["loon", "wall art"], reason="agent cleanup"
    )
    assert changes == {
        "title": ("Loon at Dusk", "Loon Wall Art"),
        "tags": (["loon"], ["loon", "wall art"]),
    }
    assert _updates(fake) == [{"title": "Loon Wall Art", "tags": ["loon", "wall art"]}]
    assert read_all(conn, "action.proposed")[0].payload["reason"] == "agent cleanup"


def test_direct_unchanged_field_is_omitted_not_an_error(conn):
    _seed(conn)
    fake = _fake()
    (_, changes), _ = _edit(conn, fake, title="Loon at Dusk", tags=["loon", "owl"])
    assert set(changes) == {"tags"}


def test_direct_noop_is_refused_and_appends_nothing(conn):
    _seed(conn)
    fake = _fake()
    with pytest.raises(EditListingError, match="no-op"):
        _edit(conn, fake, title="Loon at Dusk", description="Plain baseline.")
    assert read_all(conn, "action.") == [] and fake.calls == []


def test_direct_with_no_fields_is_refused(conn):
    _seed(conn)
    with pytest.raises(EditListingError, match="nothing to edit"):
        _edit(conn, _fake())


@pytest.mark.parametrize(
    "kw",
    [
        {"title": "x" * 141},
        {"tags": ["a,b"]},
        {"tags": ["y" * 21]},
        {"description": "d" * 5001},
    ],
)
def test_direct_invalid_fields_are_refused_never_clamped(conn, kw):
    _seed(conn)
    fake = _fake()
    with pytest.raises(EditListingError):
        _edit(conn, fake, **kw)
    assert read_all(conn, "action.") == [] and fake.calls == []


def test_direct_description_without_a_baseline_is_refused(conn):
    _seed(conn, description=None)
    with pytest.raises(EditListingError, match="description"):
        _edit(conn, _fake(), description="new")


def test_direct_unknown_listing_is_refused(conn):
    _seed(conn)
    cap = ListingSeoEdit(_fake())
    cfg = ops_config.get_ops_config(conn, USER_ID)
    with pytest.raises(EditListingError, match="snapshot"):
        run_direct_edit(conn, USER_ID, cfg, cap, 424242, title="x", live_autonomy=True)


def test_direct_bypasses_only_eligibility(conn):
    # 0 views + one tag: NOT eligible for a planner seo_edit...
    _seed(conn, views=0)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    assert str(LID) not in _eligible(conn, USER_ID, cfg)
    fake = _fake()
    (_, changes), _ = _edit(conn, fake, title="Loon Fine Art Print")
    # ...but the explicit edit still executes.
    assert changes["title"][1] == "Loon Fine Art Print"
    assert _updates(fake) == [{"title": "Loon Fine Art Print"}]


def test_materialize_can_never_emit_the_direct_flag(conn):
    _seed(conn)  # eligible: 100 views, no sales
    cap = ListingSeoEdit(_fake())
    cfg = ops_config.get_ops_config(conn, USER_ID)
    intent = ProposalIntent(
        capability_key="listing.seo_edit",
        target_id=str(LID),
        params={"title": "Loon Fine Art Print", DIRECT_PARAM: True},
        reason="x",
    )
    action = cap.materialize(conn, USER_ID, cfg, intent)
    assert action is not None and DIRECT_PARAM not in action.params


def test_direct_edit_then_undo_restores_the_original(conn):
    _seed(conn)
    fake = _fake()
    (action_id, _), cap = _edit(conn, fake, title="Loon Wall Art", description="Changed.")
    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
    assert _updates(fake)[-1] == {"title": "Loon at Dusk", "description": "Plain baseline."}
    assert read_all(conn, "action.undone")[0].payload["restored_to"] == {
        "title": "Loon at Dusk",
        "description": "Plain baseline.",
    }


def test_direct_refused_without_live_autonomy_appends_nothing(conn):
    _seed(conn)
    fake = _fake()
    with pytest.raises(EditListingError, match="live autonomy gate"):
        _edit(conn, fake, title="Loon Wall Art", live_autonomy=False)
    assert read_all(conn, "action.") == [] and fake.calls == []


def test_governor_still_applies_cooldown_blocks_a_second_same_day_edit(conn):
    _seed(conn)
    fake = _fake()
    _edit(conn, fake, title="Loon Wall Art")
    with pytest.raises(EditListingError, match="refused: ineligible"):
        _edit(conn, fake, description="Second edit same day.")
    assert len(_updates(fake)) == 1


# --- the typer verb -----------------------------------------------------------


def _cli_db(tmp_path, monkeypatch):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_AUTONOMY", raising=False)
    monkeypatch.delenv("ETSY_API_KEY", raising=False)
    c = connect(db)
    migrate(c)
    _seed(c)
    c.close()
    return db


def _open_gate(monkeypatch) -> None:
    import shopsteward.pipeline.live_gate as live_gate_mod

    monkeypatch.setattr(live_gate_mod, "live_autonomy_open", lambda: True)


def test_cli_edit_listing_prints_old_new_and_undo_restores(tmp_path, monkeypatch):
    db = _cli_db(tmp_path, monkeypatch)
    fake = _fake()
    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", lambda *, live: fake)
    _open_gate(monkeypatch)
    desc = tmp_path / "d.txt"
    desc.write_text("From a file.", encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "ops",
            "edit-listing",
            str(LID),
            "--title",
            "Loon Wall Art",
            "--tags",
            "loon, wall art",
            "--description-file",
            str(desc),
            "--reason",
            "agent edit",
            "--live-autonomy",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "action_id=" in res.output
    assert "'Loon at Dusk' -> 'Loon Wall Art'" in res.output
    assert "'Plain baseline.' -> 'From a file.'" in res.output
    assert _updates(fake) == [
        {"title": "Loon Wall Art", "tags": ["loon", "wall art"], "description": "From a file."}
    ]

    c = connect(db)
    action_id = read_all(c, "action.executed")[0].payload["action_id"]
    c.close()
    res = runner.invoke(app, ["ops", "undo", action_id, "--live-autonomy"])
    assert res.exit_code == 0, res.output
    assert _updates(fake)[-1] == {
        "title": "Loon at Dusk",
        "tags": ["loon"],
        "description": "Plain baseline.",
    }


def test_cli_edit_listing_without_live_autonomy_is_refused(tmp_path, monkeypatch):
    db = _cli_db(tmp_path, monkeypatch)
    fake = _fake()
    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", lambda *, live: fake)

    res = runner.invoke(app, ["ops", "edit-listing", str(LID), "--title", "Loon Wall Art"])

    assert res.exit_code == 1
    assert "live autonomy gate not set" in res.output
    assert fake.calls == []
    c = connect(db)
    assert read_all(c, "action.") == []
    c.close()


def test_cli_edit_listing_with_live_autonomy_and_closed_gate_never_builds_an_adapter(
    tmp_path, monkeypatch
):
    _cli_db(tmp_path, monkeypatch)

    def _boom(*, live):
        raise AssertionError("adapter must not be built when the gate is closed")

    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", _boom)
    res = runner.invoke(
        app, ["ops", "edit-listing", str(LID), "--title", "Loon Wall Art", "--live-autonomy"]
    )
    assert res.exit_code == 1
