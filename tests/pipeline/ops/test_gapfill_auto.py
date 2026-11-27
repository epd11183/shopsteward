"""Autonomous `listing.gapfill_reprint` (design 2026-11-27 §7), end to end on
FakeGelatoAdapter + FakeEtsyWriteAdapter + fakes for the inventory read, the
copy checker and the print-file host. No network, ever."""

import json
from datetime import timedelta

import pytest
from PIL import Image

from shopsteward.adapters.copy.fake import FixtureCopyAdapter
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.models import (
    EtsyListingInventory,
    EtsyListingOffering,
    EtsyListingProduct,
)
from shopsteward.adapters.pod.fake import FakeGelatoAdapter
from shopsteward.adapters.pod.interface import PodWriteError
from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.adapters.vision.fake import FakeCopyChecker
from shopsteward.adapters.vision.interface import CopyCheckResult, CopyCheckVerdict
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.projections import rebuild_mockups
from shopsteward.pipeline.listings.pod.config import get_pod_config
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.readback import run_physical_readback
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.capabilities import gapfill as gapfill_mod
from shopsteward.pipeline.ops.capabilities.gapfill import ListingGapfillReprint, _candidates
from shopsteward.pipeline.ops.governor import govern, month_spend
from shopsteward.pipeline.ops.models import ProposedAction, RefusalReason
from shopsteward.pipeline.ops.projections import rebuild_ops
from shopsteward.pipeline.ops.registry import REGISTRY, StaleTargetError
from shopsteward.pipeline.ops.runner import approve_action, run, undo_action
from shopsteward.pipeline.projections import rebuild_pipeline
from tests.pipeline.listings.helpers import seed_score
from tests.pipeline.ops.test_gapfill import (
    LISTING_SELLER,
    TODAY,
    USER_ID,
    _seed_seller_with_a_genuinely_buildable_missing_format,
)

PARTNER = [{"production_partner_id": 1, "partner_name": "scrubbed", "location": "scrubbed"}]


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_MOCKUPS_DIR", str(tmp_path / "mockups"))
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


class _Gelato(FakeGelatoAdapter):
    """Declares a production partner on the Etsy draft it 'creates' (the real
    Gelato integration does this on the Etsy side)."""

    def __init__(self, etsy: FakeEtsyWriteAdapter, *, declare_partner: bool = True, **kw) -> None:
        super().__init__(etsy_listings=etsy.listings, **kw)
        self._etsy = etsy
        self.declare_partner = declare_partner

    def get_product(self, provider_product_id):
        product = super().get_product(provider_product_id)
        if product.etsy_listing_id and self.declare_partner:
            self._etsy.listings[product.etsy_listing_id]["production_partners"] = PARTNER
        return product

    def creates(self) -> int:
        return sum(1 for name, _ in self.calls if name == "create_product")


class _Read:
    """Inventory read twin: one product per draft variant, priced at the draft's
    own retail price unless `price` overrides it."""

    def __init__(self, conn, *, price: float | None = None, drop_products: int = 0) -> None:
        self._conn, self.price, self._drop = conn, price, drop_products

    def get_listing_inventory(self, listing_id: int) -> EtsyListingInventory:
        row = self._conn.execute(
            "SELECT variants_json FROM proj_listing_drafts "
            "WHERE CAST(etsy_listing_id AS INTEGER)=?",
            (listing_id,),
        ).fetchone()
        variants = json.loads(row["variants_json"])[
            : len(json.loads(row["variants_json"])) - self._drop
        ]
        return EtsyListingInventory(
            products=[
                EtsyListingProduct(
                    product_id=i,
                    offerings=[
                        EtsyListingOffering(
                            quantity=1,
                            price=self.price if self.price is not None else v["retail_price"],
                        )
                    ],
                )
                for i, v in enumerate(variants)
            ]
        )


def _ok() -> CopyCheckResult:
    return CopyCheckResult(verdict=CopyCheckVerdict(ok=True, problems=[]))


def _world(conn, tmp_path, monkeypatch, *, declare_partner=True, price=None, gelato=None):
    """Proven seller 901 (photo p1) with acrylic/canvas/poster all missing and a
    mockup hook that seeds a completed set instead of rendering."""
    _seed_seller_with_a_genuinely_buildable_missing_format(
        conn, tmp_path, listing_id=LISTING_SELLER, photo_id="p1", file_id="f1", units=5
    )
    seed_score(conn, "p1")
    rebuild_pipeline(conn)

    hook_calls: list[str] = []

    def fake_run_mockups(c, user_id, *, photo_id, file_id):
        hook_calls.append(file_id)
        for i, intent in enumerate(("single", "digital_whatyougot")):
            path = tmp_path / "mockups_out" / f"{intent}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (50, 50), (i, i, i)).save(path, "JPEG")
            append(
                c,
                Event(
                    user_id=user_id,
                    type="mockup.generated",
                    payload={
                        "photo_id": photo_id,
                        "landing_file_id": file_id,
                        "set_key": "set-1",
                        "intent": intent,
                        "template_id": None,
                        "path": str(path),
                        "params": {},
                    },
                ),
            )
        append(
            c,
            Event(
                user_id=user_id,
                type="mockupset.completed",
                payload={
                    "photo_id": photo_id,
                    "landing_file_id": file_id,
                    "set_key": "set-1",
                    "count": 2,
                    "config_hash": "h",
                    "template_library_hash": "t",
                },
            ),
        )
        rebuild_pipeline(c)
        rebuild_mockups(c)

    monkeypatch.setattr(gapfill_mod, "mockup_hook", lambda name: fake_run_mockups)

    etsy = FakeEtsyWriteAdapter()
    gelato = gelato or _Gelato(etsy, declare_partner=declare_partner)
    checker = FakeCopyChecker([_ok()] * 10)
    cap = ListingGapfillReprint(
        FakePrintFileHost(),
        pod_adapter=gelato,
        adapter=etsy,
        read_adapter=_Read(conn, price=price),
        copy_adapter=FixtureCopyAdapter(),
        checker=checker,
        live_gelato=True,
        live_printfile=True,
        live_copy=True,
        live_check=True,
        live_etsy_read=True,
        listing_fee_usd=0.2,
    )
    cap.policy_verified = True  # E17 is not in docs yet; the shipped value is asserted below
    ops_config.seed(conn, USER_ID)
    rebuild_ops(conn)
    return cap, etsy, gelato, ops_config.get_ops_config(conn, USER_ID), hook_calls


def _go(conn, cap, cfg):
    return run(conn, USER_ID, cfg, [cap], today=TODAY, live_autonomy=True)


def _published(conn):
    return [e for e in read_all(conn, "gate3.published") if e.user_id == USER_ID]


def _acrylic_action_id(conn) -> str:
    return next(
        e.payload["action_id"]
        for e in read_all(conn, "action.proposed")
        if e.payload["params"]["product_type"] == "acrylic"
    )


# --- the happy path ----------------------------------------------------------


def test_one_proven_seller_gives_one_create_one_publish_and_020_of_spend(
    conn, tmp_path, monkeypatch
):
    cap, etsy, gelato, cfg, hook_calls = _world(conn, tmp_path, monkeypatch)

    report = _go(conn, cap, cfg)

    assert report.executed == 1
    assert gelato.creates() == 1
    problems = read_all(conn, "action.executed")[0].payload["after"]["problems"]
    assert len(_published(conn)) == 1, problems
    assert month_spend(conn, USER_ID, TODAY.isoformat()[:7], cfg) == pytest.approx(0.2)
    assert hook_calls == ["f1"]  # the mockup set was built by the preflight

    (executed,) = read_all(conn, "action.executed")
    after = executed.payload["after"]
    assert after["published"] is True and after["problems"] == []
    assert executed.payload["cost_usd"] == 0.0
    listing = etsy.listings[int(after["etsy_listing_id"])]
    assert listing["state"] == "active"
    assert listing["should_auto_renew"] is False  # step 5
    # POD-first: price is never written, and only should_auto_renew after enrich
    assert all(name != "update_listing_price" for name, _ in etsy.calls)
    assert after["provider_product_id"]


def test_second_target_same_week_is_pace_refused_and_the_operator_can_approve_it(
    conn, tmp_path, monkeypatch
):
    cap, _etsy, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch)
    report = _go(conn, cap, cfg)
    assert report.executed == 1 and report.refused >= 1

    refused = {
        e.payload["action_id"]
        for e in read_all(conn, "action.refused")
        if e.payload.get("reason") == RefusalReason.PACE.value
    }
    assert refused
    canvas_id = next(
        e.payload["action_id"]
        for e in read_all(conn, "action.proposed")
        if e.payload["params"]["product_type"] == "canvas"
    )
    assert canvas_id in refused
    assert gelato.creates() == 1  # still one create after the agent run

    # the agent path stays pace-refused; the operator's explicit approve executes
    approved = approve_action(
        conn, USER_ID, canvas_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )
    assert approved.executed == 1
    assert gelato.creates() == 2
    assert len(_published(conn)) == 2


def test_unrelated_existing_pod_draft_is_untouched_by_the_chain(conn, tmp_path, monkeypatch):
    # An unlinked POD draft for another photo (what `shop build` leaves behind):
    # unfiltered, link_pod_drafts would attempt it too -- the fan-out bug.
    cap, _etsy, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch)
    pod_hash = gapfill_mod.pod_config.pod_config_hash(get_pod_config(conn, USER_ID))
    for event_type, payload in (
        (
            "listingdraft.created",
            {
                "draft_id": "unrelated",
                "landing_file_id": "f1",
                "photo_id": "p2",
                "set_key": None,
                "provider": "gelato",
                "format": "poster",
                "sku_source": "provider",
                "listing_type": "physical",
                "config_hash": None,
                "pod_config_hash": pod_hash,
            },
        ),
        (
            "listingdraft.print_file_hosted",
            {
                "draft_id": "unrelated",
                "host": "fake",
                "file_key": "k",
                "expires_at": "2099-01-01T00:00:00Z",
                "sha256": "s",
            },
        ),
    ):
        append(conn, Event(user_id=USER_ID, type=event_type, payload=payload))
    rebuild_listings(conn)
    assert conn.execute(
        "SELECT 1 FROM proj_listing_drafts "
        "WHERE draft_id='unrelated' AND print_file_key IS NOT NULL"
    ).fetchone()

    _go(conn, cap, cfg)

    assert gelato.creates() == 1
    touched = [
        e
        for e in read_all(conn, "listingdraft.provider_")
        if e.payload.get("draft_id") == "unrelated"
    ]
    assert touched == []  # no attempt, create, link or failure event


def test_link_failure_then_retry_creates_exactly_one_product(conn, tmp_path, monkeypatch):
    etsy = FakeEtsyWriteAdapter()
    slow = _Gelato(etsy, links_after_polls=1000)  # poll budget exhausts: created, not linked
    cap, _e, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch, gelato=slow)
    cap._adapter = etsy  # the same Etsy fake the slow Gelato seeds

    first = _go(conn, cap, cfg)
    assert first.executed == 0 and first.refused >= 1  # non-terminal refusal
    assert gelato.creates() == 1
    assert _published(conn) == []

    gelato._links_after_polls = 1  # Gelato catches up
    action_id = _acrylic_action_id(conn)
    retried = approve_action(
        conn, USER_ID, action_id, [cap], cfg=cfg, today=TODAY, live_autonomy=True
    )

    assert retried.executed == 1
    assert gelato.creates() == 1  # re-polled, never re-created
    assert len(_published(conn)) == 1


def test_resuming_an_attempted_draft_is_never_pace_refused(conn, tmp_path, monkeypatch):
    etsy = FakeEtsyWriteAdapter()
    slow = _Gelato(etsy, links_after_polls=1000)
    cap, _e, _g, cfg, _ = _world(conn, tmp_path, monkeypatch, gelato=slow)
    cap._adapter = etsy
    _go(conn, cap, cfg)  # week's one create is attempted, link pending
    action = next(
        a for a in _candidates(conn, USER_ID, cfg).values() if a.params["product_type"] == "acrylic"
    )
    other = next(
        a for a in _candidates(conn, USER_ID, cfg).values() if a.params["product_type"] == "canvas"
    )

    assert govern(conn, USER_ID, action, cap, cfg, TODAY, record=False).approved
    refused = govern(conn, USER_ID, other, cap, cfg, TODAY, record=False)
    assert refused.reason == RefusalReason.PACE  # a NEW product waits (week full + in flight)


def test_an_attempted_but_unconfirmed_create_is_never_recreated(conn, tmp_path, monkeypatch):
    class _Timeout(_Gelato):
        def create_product(self, spec):
            self.calls.append(("create_product", {}))
            raise PodWriteError(0, "transport error: ReadTimeout")

    etsy = FakeEtsyWriteAdapter()
    cap, _e, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch, gelato=_Timeout(etsy))
    cap._adapter = etsy

    first = _go(conn, cap, cfg)
    assert first.executed == 1  # executed, unpublished: NEEDS YOU
    (executed,) = read_all(conn, "action.executed")
    assert executed.payload["after"]["published"] is False
    assert "Gelato dashboard" in executed.payload["after"]["problems"][0]

    # never a candidate again, and execute() refuses to run it
    draft_id = executed.payload["after"]["draft_id"]
    assert all(a.params["draft_id"] != draft_id for a in _candidates(conn, USER_ID, cfg).values())
    acrylic = ProposedAction.model_validate(
        next(
            e.payload
            for e in read_all(conn, "action.proposed")
            if e.payload["params"]["product_type"] == "acrylic"
        )
    )
    with pytest.raises(StaleTargetError):
        cap.execute(conn, USER_ID, acrylic)
    assert gelato.creates() == 1


# --- the read-back gate --------------------------------------------------------


def test_a_below_floor_inventory_price_keeps_the_draft_a_draft(conn, tmp_path, monkeypatch):
    cap, etsy, _g, cfg, _ = _world(conn, tmp_path, monkeypatch, price=1.0)

    _go(conn, cap, cfg)

    (executed,) = read_all(conn, "action.executed")
    after = executed.payload["after"]
    assert after["published"] is False
    assert any("below floor" in p for p in after["problems"])
    assert etsy.listings[int(after["etsy_listing_id"])]["state"] == "draft"
    assert _published(conn) == []
    assert month_spend(conn, USER_ID, TODAY.isoformat()[:7], cfg) == 0.0


def test_an_empty_production_partner_keeps_the_draft_a_draft(conn, tmp_path, monkeypatch):
    cap, etsy, _g, cfg, _ = _world(conn, tmp_path, monkeypatch, declare_partner=False)

    _go(conn, cap, cfg)

    (executed,) = read_all(conn, "action.executed")
    after = executed.payload["after"]
    assert after["published"] is False
    assert any("production partner" in p for p in after["problems"])
    assert etsy.listings[int(after["etsy_listing_id"])]["state"] == "draft"
    assert _published(conn) == []
    # NEEDS YOU: the brief reads this failed read-back
    assert [e.payload["ok"] for e in read_all(conn, "listingdraft.readback")][-1] is False


def test_a_digital_whatyougot_image_or_digital_title_fails_the_gate(conn, tmp_path, monkeypatch):
    cap, etsy, _g, cfg, _ = _world(conn, tmp_path, monkeypatch, declare_partner=False)
    _go(conn, cap, cfg)
    (executed,) = read_all(conn, "action.executed")
    draft_id = executed.payload["after"]["draft_id"]
    listing_id = int(executed.payload["after"]["etsy_listing_id"])

    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.images_selected",
            payload={
                "draft_id": draft_id,
                "images": [{"path": "x.jpg", "intent": "digital_whatyougot", "rank": 1}],
            },
        ),
    )
    rebuild_listings(conn)
    etsy.listings[listing_id]["title"] += " (Digital Download)"
    etsy.listings[listing_id]["production_partners"] = PARTNER

    result = run_physical_readback(
        conn,
        USER_ID,
        draft_id,
        adapter=etsy,
        read_adapter=_Read(conn),
        checker=FakeCopyChecker([_ok()]),
        model="m",
        pod_cfg=get_pod_config(conn, USER_ID),
    )

    assert result.ok is False
    assert any("digital_whatyougot" in p for p in result.problems)
    assert any("Digital Download" in p for p in result.problems)


def test_a_missing_inventory_product_fails_the_gate(conn, tmp_path, monkeypatch):
    cap, _etsy, _g, cfg, _ = _world(conn, tmp_path, monkeypatch)
    cap._read = _Read(conn, drop_products=1)

    _go(conn, cap, cfg)

    (executed,) = read_all(conn, "action.executed")
    assert executed.payload["after"]["published"] is False
    assert any("inventory products" in p for p in executed.payload["after"]["problems"])


# --- undo ------------------------------------------------------------------------


def test_undo_deactivates_the_listing_and_never_deletes_the_gelato_product(
    conn, tmp_path, monkeypatch
):
    cap, etsy, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch)
    _go(conn, cap, cfg)
    action_id = _acrylic_action_id(conn)
    (executed,) = read_all(conn, "action.executed")
    listing_id = int(executed.payload["after"]["etsy_listing_id"])
    assert etsy.listings[listing_id]["state"] == "active"

    undo_action(conn, USER_ID, action_id, [cap], live_autonomy=True)

    assert etsy.listings[listing_id]["state"] == "inactive"
    assert all(name != "delete_product" for name, _ in gelato.calls)


# --- governance -------------------------------------------------------------------


def test_shipped_policy_unverified_refuses_every_gapfill_action(conn, tmp_path, monkeypatch):
    cap, _e, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch)
    cap.policy_verified = ListingGapfillReprint.policy_verified
    assert cap.policy_verified is False

    report = _go(conn, cap, cfg)

    assert report.executed == 0 and gelato.creates() == 0
    reasons = {e.payload["reason"] for e in read_all(conn, "action.refused")}
    assert reasons == {RefusalReason.POLICY_UNVERIFIED.value}


def test_closed_live_gates_refuse_with_precondition(conn, tmp_path, monkeypatch):
    cap, _e, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch)
    cap.precondition_ok = False

    _go(conn, cap, cfg)

    assert gelato.creates() == 0
    reasons = {e.payload["reason"] for e in read_all(conn, "action.refused")}
    assert reasons == {RefusalReason.PRECONDITION.value}


def test_attempt_cap_writes_a_needs_you_line_and_stops_proposing(conn, tmp_path, monkeypatch):
    etsy = FakeEtsyWriteAdapter()
    slow = _Gelato(etsy, links_after_polls=1000)
    cap, _e, _g, cfg, _ = _world(conn, tmp_path, monkeypatch, gelato=slow)
    cap._adapter = etsy
    action = next(
        a for a in cap.propose(conn, USER_ID, cfg) if a.params["product_type"] == "acrylic"
    )
    for day in range(cfg.gapfill.max_attempts):
        a = action.model_copy(update={"action_id": f"{action.action_id[:-1]}{day}"})
        append(conn, Event(user_id=USER_ID, type="action.proposed", payload=a.model_dump()))
        with pytest.raises(RuntimeError):
            cap.execute(conn, USER_ID, a)
        append(
            conn,
            Event(
                user_id=USER_ID,
                type="action.refused",
                payload={"action_id": a.action_id, "reason": "execute_revalidation_error"},
            ),
        )
    last = [e for e in read_all(conn, "listingdraft.readback") if not e.payload["ok"]][-1]
    assert "gave up" in last.payload["problems"][0]
    assert all(
        c.params["product_type"] != "acrylic" for c in _candidates(conn, USER_ID, cfg).values()
    )


def test_no_credential_or_signed_url_in_any_event_and_user_id_everywhere(
    conn, tmp_path, monkeypatch
):
    cap, _e, _g, cfg, _ = _world(conn, tmp_path, monkeypatch)
    _go(conn, cap, cfg)

    banned = ("api_key", "apikey", "secret", "signed_url", "access_token", "refresh_token")
    for e in read_all(conn):
        blob = json.dumps(e.payload).lower()
        for word in banned:
            assert word not in blob, f"event {e.type} leaked {word!r}"
        assert e.user_id == USER_ID


def test_date_helpers_are_week_scoped(conn, tmp_path, monkeypatch):
    cap, _e, _g, cfg, _ = _world(conn, tmp_path, monkeypatch)
    _go(conn, cap, cfg)
    other = next(
        a for a in _candidates(conn, USER_ID, cfg).values() if a.params["product_type"] == "canvas"
    )
    # the same attempt does not count against a different ISO week
    assert govern(conn, USER_ID, other, cap, cfg, TODAY, record=False).reason == RefusalReason.PACE
    next_week = TODAY + timedelta(days=7)
    assert gapfill_mod.pace_exceeded(conn, USER_ID, other, cfg, next_week) is False


def test_an_unexpected_error_after_enrich_closes_the_draft_instead_of_raising(
    conn, tmp_path, monkeypatch
):
    cap, etsy, _g, cfg, _ = _world(conn, tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise KeyError("not an EtsyWriteError")

    monkeypatch.setattr(gapfill_mod.gate3, "publish", boom)

    report = _go(conn, cap, cfg)

    assert report.executed == 1  # recorded, never raised past a live Etsy draft
    (executed,) = read_all(conn, "action.executed")
    after = executed.payload["after"]
    assert after["published"] is False
    assert "post-enrich step failed" in after["problems"][0]
    assert etsy.listings[int(after["etsy_listing_id"])]["state"] == "draft"
    # the latest read-back is the failure, so the brief lists it under NEEDS YOU
    assert read_all(conn, "listingdraft.readback")[-1].payload["ok"] is False


def test_an_unexpected_error_before_the_provider_call_is_a_counted_refusal(
    conn, tmp_path, monkeypatch
):
    cap, _etsy, gelato, cfg, _ = _world(conn, tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise KeyError("mockup hook exploded")

    monkeypatch.setattr(gapfill_mod, "mockup_hook", lambda name: boom)

    report = _go(conn, cap, cfg)

    assert report.executed == 0
    assert gelato.creates() == 0
    reasons = {e.payload["reason"] for e in read_all(conn, "action.refused")}
    assert "execute_revalidation_error" in reasons
