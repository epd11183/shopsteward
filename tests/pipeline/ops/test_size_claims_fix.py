"""`listing.size_claims_fix` (slice 5a): description-only proposal for backfilled
listings. Fakes only, synthetic copy."""

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.live import _encode_form_data
from shopsteward.adapters.etsy.models import EtsyListingUpdate
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings import push as push_mod
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities.size_claims_fix import ListingSizeClaimsFix
from shopsteward.pipeline.ops.models import Tier
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, StaleTargetError, register
from shopsteward.pipeline.ops.runner import (
    LIVE_GATED_CAPABILITIES,
    approve_action,
    run,
    undo_action,
)
from tests.pipeline.listings.test_slice4_backfill import LID, _run, _world
from tests.pipeline.ops.helpers import USER_ID

TODAY = datetime.now(UTC).date()
OLD_SENTENCE = (
    "The photograph itself is the artist's original work and is never AI-generated or AI-edited."
)
# plan (600x400 @30ppi): full 4x6/8x12/12x18, 4x5/8x10, 5x7, 11x14, A4/A3
DESC = (
    "Room scenes are AI-generated staging mockups. " + OLD_SENTENCE + "\n"
    "Includes these print sizes:\n"
    "4x6 · 8x10 · 11x14 · 16x20 · A2 · A3\n"
    "Thanks for visiting!"
)
CLIPPED = "8x10 · 11x14 · A3"  # 4x6 only survives when the rank-1 file is the source itself


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _observe(conn, description, state="active"):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="etsy.listing.observed",
            payload={
                "listing_id": LID,
                "title": "Misty Ridge",
                "state": state,
                "quantity": 999,
                "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"},
                "tags": ["wall art"],
                **({"description": description} if description is not None else {}),
            },
        ),
    )


def _setup(conn, tmp_path, description=DESC, same_bytes=False):
    adapter = _world(conn, tmp_path, legacy=not same_bytes)
    if same_bytes:
        adapter.upload_listing_file(
            LID, (tmp_path / "photo-1.jpg").read_bytes(), name="photo-1.jpg", rank=1
        )
    assert _run(conn, adapter).results[0].status == "uploaded"
    _observe(conn, description)
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    adapter.calls.clear()
    return adapter, ops_config.get_ops_config(conn, USER_ID)


def _propose(conn, cfg, adapter):
    return ListingSizeClaimsFix(adapter).propose(conn, USER_ID, cfg)


def test_propose_builds_description_only_action(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    [a] = _propose(conn, cfg, adapter)
    new = a.params["new_description"]
    assert a.capability == "listing.size_claims_fix" and a.target_id == str(LID)
    assert a.params["old_description"] == DESC and a.params["listing_id"] == LID
    assert a.estimated_cost_usd == 0.0 and a.undo_available
    # rank-1 is not the source -> the full entry's 4x6 is NOT delivered -> removed
    assert CLIPPED in new and "4x6" not in new and "16x20" not in new and "A2" not in new
    assert "it is not AI-generated, and AI is never used to add anything to it." in new
    assert new.endswith("\nThanks for visiting!") and "Room scenes are AI-generated" in new
    assert "16x20" in a.reason


def test_full_sizes_count_when_rank1_is_the_source_file(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path, same_bytes=True)
    [a] = _propose(conn, cfg, adapter)
    assert "4x6 · 8x10 · 11x14 · A3" in a.params["new_description"]


def test_tier_is_auto_since_2026_11_22_and_live_gated():
    cap = ListingSizeClaimsFix(FakeEtsyWriteAdapter())
    assert cap.max_tier == Tier.AUTO  # operator decision 2026-11-22: no approval
    assert cap.key in LIVE_GATED_CAPABILITIES


def test_no_proposal_when_backfill_incomplete(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    crop = next(f for f in adapter.list_listing_files(LID) if f.rank == 3)
    adapter.delete_listing_file(LID, crop.listing_file_id)
    assert _propose(conn, cfg, adapter) == []


@pytest.mark.parametrize(
    "description", [None, "Lovely print.\nPrints at 8x10 and A3.", "Prose says 16x20 here."]
)
def test_no_proposal_without_baseline_fixable_claim_or_when_hand_edit(conn, tmp_path, description):
    adapter, cfg = _setup(conn, tmp_path, description=description)
    assert _propose(conn, cfg, adapter) == []


def test_no_proposal_for_disclosure_only_difference(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path, description=f"Intro.\n{OLD_SENTENCE}\n8x10 · A3")
    assert _propose(conn, cfg, adapter) == []


def test_propose_never_raises_when_file_list_unreadable(conn, tmp_path):
    _, cfg = _setup(conn, tmp_path)
    assert _propose(conn, cfg, FakeEtsyWriteAdapter()) == []  # fresh fake: 404


def test_execute_sends_description_only_and_undo_restores(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    cap = ListingSizeClaimsFix(adapter)
    register(cap)
    run(conn, USER_ID, cfg, [cap])
    [proposed] = read_all(conn, "action.proposed")
    action_id = proposed.payload["action_id"]
    assert proposed.payload["tier"] == int(Tier.AUTO)

    report = approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, live_autonomy=True)
    assert report.executed == 1
    updates = [c for c in adapter.calls if c[0] == "update_listing"]
    assert len(updates) == 1
    fields = updates[0][1]["fields"]
    assert set(fields) == {"description"}
    new = fields["description"]
    assert adapter.listings[LID]["description"] == new != DESC
    assert _encode_form_data(EtsyListingUpdate(description=new)) == {"description": new}

    # snapshot is stale until the next sync: the same fix is not re-proposed
    assert _propose(conn, cfg, adapter) == []

    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
    assert adapter.listings[LID]["description"] == DESC
    updates = [c for c in adapter.calls if c[0] == "update_listing"]
    assert updates[1][1]["fields"] == {"description": DESC}


def test_execute_refuses_when_description_changed(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    cap = ListingSizeClaimsFix(adapter)
    [a] = _propose(conn, cfg, adapter)
    _observe(conn, DESC + "\nHand-added line.")
    with pytest.raises(StaleTargetError):
        cap.execute(conn, USER_ID, a)
    assert not [c for c in adapter.calls if c[0] == "update_listing"]


def test_pending_seo_edit_for_same_listing_does_not_collide(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="action.proposed",
            payload={
                "action_id": "seo-1",
                "capability": "listing.seo_edit",
                "target_type": "listing",
                "target_id": str(LID),
                "tier": int(Tier.PROPOSE),
                "reason": "x",
                "inputs_hash": "h",
                "undo_available": True,
                "expires_at": "2999-01-01",
                "params": {},
            },
        ),
    )
    report = run(conn, USER_ID, cfg, [ListingSizeClaimsFix(adapter)])
    assert report.proposed == 1 and report.skipped_duplicate_target == 0


def test_cli_capability_flag_runs_only_this_propose(conn, tmp_path, monkeypatch):
    adapter, _ = _setup(conn, tmp_path)
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    monkeypatch.setenv("SHOPSTEWARD_DB", db)
    monkeypatch.delenv("SHOPSTEWARD_LIVE_AUTONOMY", raising=False)
    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", lambda *, live: adapter)

    result = CliRunner().invoke(
        app, ["ops", "run", "--no-dry-run", "--capability", "listing.size_claims_fix"]
    )
    assert result.exit_code == 0, result.output
    assert "planner skipped" in result.output
    c = connect(db)
    assert [e.payload["capability"] for e in read_all(c, "action.proposed")] == [
        "listing.size_claims_fix"
    ]
    assert not [x for x in adapter.calls if x[0] == "update_listing"]

    bad = CliRunner().invoke(app, ["ops", "run", "--capability", "nope"])
    assert bad.exit_code == 1


def test_undo_refuses_when_description_edited_since(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path)
    cap = ListingSizeClaimsFix(adapter)
    register(cap)
    run(conn, USER_ID, cfg, [cap])
    action_id = read_all(conn, "action.proposed")[0].payload["action_id"]
    approve_action(conn, USER_ID, action_id, [cap], cfg=cfg, live_autonomy=True)
    _observe(conn, "Someone rewrote this by hand.")
    with pytest.raises(StaleTargetError):
        undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)
    assert len([c for c in adapter.calls if c[0] == "update_listing"]) == 1


def _escaped(text):
    return text.replace("&", "&amp;").replace("'", "&#39;").replace('"', "&quot;")


ENTITY_DESC = DESC.replace("Room scenes", 'Fish & chips "fresh" room scenes', 1)


def test_entities_are_unescaped_for_preview_update_stale_check_and_undo(conn, tmp_path):
    adapter, cfg = _setup(conn, tmp_path, description=_escaped(ENTITY_DESC))
    assert "&#39;" in _escaped(ENTITY_DESC)
    cap = ListingSizeClaimsFix(adapter)
    register(cap)
    run(conn, USER_ID, cfg, [cap])
    [proposed] = read_all(conn, "action.proposed")
    params = proposed.payload["params"]
    assert params["old_description"] == ENTITY_DESC  # readable preview
    assert "&" in params["new_description"] and "&#" not in params["new_description"]
    # the disclosure sentence arrived as artist&#39;s and was still swapped
    assert "it is not AI-generated" in params["new_description"]
    assert "never AI-generated or AI-edited" not in params["new_description"]

    # observed text is only the escaped form of `old`: the stale check passes
    report = approve_action(
        conn, USER_ID, proposed.payload["action_id"], [cap], cfg=cfg, live_autonomy=True
    )
    assert report.executed == 1
    sent = [c for c in adapter.calls if c[0] == "update_listing"][0][1]["fields"]["description"]
    assert sent == params["new_description"]
    assert "&#" not in sent and "&amp;" not in sent and "&quot;" not in sent
    assert 'Fish & chips "fresh"' in sent

    undo_action(conn, USER_ID, proposed.payload["action_id"], [cap], live_autonomy=True)
    assert adapter.listings[LID]["description"] == ENTITY_DESC
