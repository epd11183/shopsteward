"""Slice 3b (design 2026-11-21-multi-ratio-digital-files §7): copy sizes come
from the file plan, deterministic Files-included block, size guard, per-photo
"what you get" panel, build refuses a stale panel."""

import json

import pytest
from PIL import Image

from shopsteward.adapters.copy.interface import (
    CopyInputs,
    CopyParseError,
    CopyResult,
    CopyUsage,
    CopyVerdict,
)
from shopsteward.adapters.copy.openrouter import OpenRouterCopyAdapter
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.adapters.etsy.interface import EtsyWriteError
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.mockups.jobs import run_mockups
from shopsteward.pipeline.listings import config as listing_config
from shopsteward.pipeline.listings import drafts as drafts_mod
from shopsteward.pipeline.listings.copy import (
    _CONFIG_DEFAULTS_DIR,
    MAX_COPY_REFUSALS,
    generate_copy,
    load_wording,
)
from shopsteward.pipeline.listings.digital_files import plan_files, plan_sizes
from shopsteward.pipeline.listings.drafts import build_drafts
from shopsteward.pipeline.listings.gate3 import retry_push
from shopsteward.pipeline.listings.models import ListingImage
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.listings.push import push_drafts
from shopsteward.pipeline.listings.size_guard import undelivered_sizes
from shopsteward.pipeline.projections import rebuild_pipeline

from .helpers import USER_ID, seed_landing_file_with_mockup_set, seed_score

DRAFT_ID = "draft-1"
IMAGE = ListingImage(path="/mockups/single.jpg", intent="single", rank=1)

BLOCK_24MP = (
    "Files included (5 print-ready JPEG files):\n"
    "• Full frame (2:3) — prints 4x6, 8x12, 12x18 (300 PPI)\n"
    "• 4:5 — prints 4x5, 8x10 (300 PPI)\n"
    "• 5:7 — prints 5x7 (300 PPI)\n"
    "• 11:14 — prints 11x14 (300 PPI)\n"
    "• A-series — prints A4, A3 (300 PPI)"
)
BLOCK_45MP = (
    "Files included (5 print-ready JPEG files):\n"
    "• Full frame (2:3) — prints 4x6, 8x12, 12x18, 16x24 (300 PPI)\n"
    "• 4:5 — prints 4x5, 8x10, 16x20 (300 PPI)\n"
    "• 5:7 — prints 5x7 (300 PPI)\n"
    "• 11:14 — prints 11x14 (300 PPI)\n"
    "• A-series — prints A4, A3, A2 (300 PPI)"
)


class RecordingAdapter:
    def __init__(self, *verdicts: CopyVerdict) -> None:
        self.verdicts = list(verdicts)
        self.inputs: list[CopyInputs] = []

    def generate_copy(self, inputs: CopyInputs, *, model: str) -> CopyResult:
        self.inputs.append(inputs)
        return CopyResult(verdict=self.verdicts.pop(0))


def _verdict(description: str = "A quiet ridge at dawn.", title: str = "Misty Ridge Wall Art"):
    return CopyVerdict(title=title, tags=[f"tag{i}" for i in range(13)], description=description)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


@pytest.fixture()
def cfg(conn):
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    rebuild_pipeline(conn)
    return listing_config.get_config(conn, USER_ID)


def _generate(conn, cfg, adapter, plan):
    seed_score(conn, "photo-1")
    rebuild_pipeline(conn)
    return generate_copy(
        conn, USER_ID, DRAFT_ID, "f" * 64, "photo-1", [IMAGE], adapter, cfg,
        live=False, soft_cap_usd=10.0, plan=plan,
    )  # fmt: skip


def _description(conn) -> str:
    events = [e for e in read_all(conn, "listingdraft.copy_generated") if e.user_id == USER_ID]
    return events[-1].payload["description"]


@pytest.mark.parametrize(
    ("dims", "block"), [((6000, 4000), BLOCK_24MP), ((8256, 5504), BLOCK_45MP)]
)
def test_copy_files_block_comes_from_plan_and_prompt_gets_no_sizes(conn, cfg, dims, block):
    plan = plan_files(dims, None, cfg.digital_files)
    adapter = RecordingAdapter(_verdict())
    assert _generate(conn, cfg, adapter, plan) is True
    assert adapter.inputs[0].sizes == []
    assert block in _description(conn)
    assert len(plan.files) == 5


def test_files_block_for_file_without_sizes(conn, cfg):
    plan = plan_files((4000, 4000), None, cfg.digital_files)  # square: not 2:3
    adapter = RecordingAdapter(_verdict())
    assert _generate(conn, cfg, adapter, plan) is True
    assert "• Full frame — for screens and custom sizes" in _description(conn)
    assert plan_sizes(plan)  # other files still deliver sizes


@pytest.mark.parametrize("claim", ["16x20", "16 x 20", "20x16", "A2", "a2"])
def test_guard_refuses_undelivered_size_retryably(conn, cfg, claim):
    plan = plan_files((6000, 4000), None, cfg.digital_files)  # no 16x20, no A2
    adapter = RecordingAdapter(
        _verdict(description=f"Print it at {claim} for your wall."),
        _verdict(description="Print it at 8x10 for your wall."),
    )
    assert _generate(conn, cfg, adapter, plan) is False
    assert read_all(conn, "listingdraft.copy_generated") == []
    # retryable: the next run's call succeeds
    assert _generate(conn, cfg, adapter, plan) is True


def test_guard_checks_title_and_tags(conn, cfg):
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    bad_tags = _verdict()
    bad_tags.tags[0] = "16x20 print"
    assert _generate(conn, cfg, RecordingAdapter(_verdict(title="Ridge 16x20")), plan) is False
    assert _generate(conn, cfg, RecordingAdapter(bad_tags), plan) is False


def test_guard_allows_delivered_sizes_and_block_sizes(conn, cfg):
    plan = plan_files((8256, 5504), None, cfg.digital_files)
    adapter = RecordingAdapter(_verdict(description="Prints to 16x20 and A2 and 8 x 10."))
    assert _generate(conn, cfg, adapter, plan) is True


def test_no_plan_copy_is_unchanged(conn, cfg):
    adapter = RecordingAdapter(_verdict(description="Print it at 16x20."))
    assert _generate(conn, cfg, adapter, None) is True
    assert adapter.inputs[0].sizes == ["4x5", "5x7", "8x10", "11x14", "16x20", "A2", "A3", "A4"]
    assert _description(conn).startswith("Print it at 16x20.")
    assert "Files included" not in _description(conn)


def test_prompt_template_drops_sizes_when_none():
    template = (_CONFIG_DEFAULTS_DIR / "prompts" / "listing_copy.txt").read_text(encoding="utf-8")
    adapter = OpenRouterCopyAdapter(
        api_key="k", prompt_template=template, sizes_wording=load_wording()
    )
    base = dict(house_style="hs", orientation="landscape", formats=["JPEG"])
    with_plan = adapter._render_prompt(CopyInputs(sizes=[], **base))
    legacy = adapter._render_prompt(CopyInputs(sizes=["4x5", "8x10"], **base))
    assert "available sizes" not in with_plan and "4x5" not in with_plan
    assert "Do NOT mention any print size" in with_plan
    assert "4x5, 8x10" in legacy


# --- panel + build ---------------------------------------------------------


def _photo(tmp_path, size=(600, 400)):
    path = tmp_path / "photo-1.jpg"
    Image.new("RGB", size, (10, 20, 30)).save(path, "JPEG")
    return path


def test_run_mockups_panel_renders_and_records_plan_sizes(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "none"))
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    path = tmp_path / "m.tif"
    Image.new("RGB", (6000, 4000), (150, 130, 110)).save(path, format="TIFF")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": "a" * 64, "path": str(path), "base_name": None, "format": "TIFF",
                "width": 6000, "height": 4000, "color_space": "RGB", "photo_id": "p",
            },
        ),
    )  # fmt: skip
    rebuild_pipeline(conn)
    run_mockups(conn, USER_ID, output_dir=tmp_path / "out")
    row = conn.execute(
        "SELECT params_json FROM proj_mockups WHERE intent='digital_whatyougot'"
    ).fetchone()
    expected = plan_sizes(
        plan_files((6000, 4000), None, listing_config.get_config(conn, USER_ID).digital_files)
    )
    assert "16x20" not in expected
    assert json.loads(row["params_json"])["sizes"] == expected


def _seed_with_sizes(conn, tmp_path, sizes):
    """sizes: list -> panel records it; None -> legacy panel (params {}); "plan" -> the
    sizes the 600x400 plan delivers at ppi 30."""
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 30)
    rebuild_listings(conn)
    cfg = listing_config.get_config(conn, USER_ID)
    if sizes == "plan":
        sizes = plan_sizes(plan_files((600, 400), None, cfg.digital_files))
    path = _photo(tmp_path)
    seed_landing_file_with_mockup_set(
        conn, file_id="f" * 64, photo_id="photo-1", path=str(path), set_key="set-1",
        intents=["single", "digital_whatyougot"], mockups_dir=tmp_path / "mockups",
        panel_params={} if sizes is None else {"sizes": sizes},
    )  # fmt: skip
    return cfg


def test_build_skips_draft_when_panel_sizes_mismatch(conn, tmp_path):
    _seed_with_sizes(conn, tmp_path, ["4x5", "16x20"])
    report = build_drafts(conn, USER_ID)
    assert report.drafts_built == 0 and report.skipped_panel_stale == 1
    assert report.legacy_panels == 0
    assert "panel sizes stale: re-render mockups" in report.skip_reasons[0]
    assert read_all(conn, "listingdraft.created") == []


def test_build_skips_legacy_panel_without_sizes_and_counts_it(conn, tmp_path):
    _seed_with_sizes(conn, tmp_path, None)
    report = build_drafts(conn, USER_ID)
    assert (report.drafts_built, report.skipped_panel_stale, report.legacy_panels) == (0, 1, 1)
    assert "legacy panel" in report.skip_reasons[0]


def test_build_proceeds_when_panel_matches_plan(conn, tmp_path):
    cfg = _seed_with_sizes(conn, tmp_path, "plan")
    report = build_drafts(conn, USER_ID)
    assert report.skipped_panel_stale == 0 and report.drafts_built == 1
    description = _description_for_build(conn)
    assert "Files included (5 print-ready JPEG files):" in description
    assert cfg.digital_files is not None


def test_legacy_panel_builds_when_digital_files_unset(conn, tmp_path):
    _seed_with_sizes(conn, tmp_path, None)
    listing_config.set_value(conn, USER_ID, "digital_files", None)
    report = build_drafts(conn, USER_ID)
    assert report.drafts_built == 1 and report.skipped_panel_stale == 0
    assert "Files included" not in _description_for_build(conn)


def _description_for_build(conn) -> str:
    return [e for e in read_all(conn, "listingdraft.copy_generated") if e.user_id == USER_ID][
        -1
    ].payload["description"]


def test_full_digital_prompt_has_no_size_instruction_and_names_the_rule(conn, cfg):
    prompts = _CONFIG_DEFAULTS_DIR / "prompts"
    template = (prompts / "listing_copy.txt").read_text(encoding="utf-8")
    adapter = OpenRouterCopyAdapter(
        api_key="k", prompt_template=template, sizes_wording=load_wording()
    )
    recorder = RecordingAdapter(_verdict())
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    assert _generate(conn, cfg, recorder, plan) is True
    prompt = adapter._render_prompt(recorder.inputs[0])
    assert "Do NOT mention any print size" in prompt
    assert "follow the prompt's sizes rule" in prompt
    assert "available sizes" not in prompt
    assert undelivered_sizes([prompt], plan, cfg.digital_files) == []
    assert "size ladder" not in prompt.lower()
    assert "(2:3 Digital Download)" not in prompt


# --- size guard ---------------------------------------------------------------

_CAUGHT = [
    '16"x20"', "16″×20″", "16x20in", "16 x 20 inches", "40x60cm", "100x150 mm",
    "8.5x11", "16x20", "16 x 20", "20x16", "A2", "a2", "11x17", "16 by 20", "3x5",
]  # fmt: skip
_IGNORED = [
    "2:3", "4K", "R5", "5x zoom", "1920x1080", "2x3", "4x4", "A3", "8x10", "8 by 10 in",
    "210x297mm", "21x29.7cm", "the Canon R5 and a 5x telephoto", "8 by 10",
]  # fmt: skip


@pytest.mark.parametrize("text", _CAUGHT)
def test_guard_catches_variants(cfg, text):
    plan = plan_files((6000, 4000), None, cfg.digital_files)  # 4x5/8x10/A4/A3, no 16x20/A2
    assert undelivered_sizes([f"Prints at {text} fit."], plan, cfg.digital_files), text


@pytest.mark.parametrize("text", _IGNORED)
def test_guard_ignores_non_print_sizes_and_delivered_ones(cfg, text):
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    assert undelivered_sizes([f"Shot with {text} here."], plan, cfg.digital_files) == []


def test_guard_reports_decimal_pair_whole_and_a_series_rule(cfg):
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    assert undelivered_sizes(["8.5x11"], plan, cfg.digital_files) == ["8.5x11"]
    # documented: any A0-A6 token the plan doesn't deliver is flagged ("A1 road" too)
    assert undelivered_sizes(["A1 road"], plan, cfg.digital_files) == ["A1"]
    assert undelivered_sizes(["40x60cm"], plan, cfg.digital_files) == ["40x60cm"]


# --- refusal accounting -------------------------------------------------------


class UsageAdapter(RecordingAdapter):
    def generate_copy(self, inputs, *, model):
        r = super().generate_copy(inputs, model=model)
        usage = CopyUsage(model=model, input_tokens=10, output_tokens=5, est_cost_usd=0.01)
        return CopyResult(verdict=r.verdict, usage=usage)


def test_refused_copy_records_llm_call_and_refusal_event(conn, cfg):
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    adapter = UsageAdapter(_verdict(description="Prints at 16x20."))
    assert _generate(conn, cfg, adapter, plan) is False
    assert len([e for e in read_all(conn, "llm.call") if e.user_id == USER_ID]) == 1
    assert read_all(conn, "copy.refused")[0].payload["offending"] == ["16x20"]
    assert read_all(conn, "listingdraft.copy_generated") == []


def test_success_records_exactly_one_llm_call(conn, cfg):
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    assert _generate(conn, cfg, UsageAdapter(_verdict()), plan) is True
    assert len(read_all(conn, "llm.call")) == 1


def test_refusal_cap_stops_the_llm_and_force_resets(conn, cfg):
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    bad = [_verdict(description="Prints at 16x20.") for _ in range(MAX_COPY_REFUSALS + 2)]
    adapter = UsageAdapter(*bad)
    for _ in range(MAX_COPY_REFUSALS):
        assert _generate(conn, cfg, adapter, plan) is False
    assert _generate(conn, cfg, adapter, plan) is False
    assert len(adapter.inputs) == MAX_COPY_REFUSALS  # the 4th call never reached the LLM
    # a --force rebuild re-emits listingdraft.created for the draft: counter resets
    append(
        conn, Event(user_id=USER_ID, type="listingdraft.created", payload={"draft_id": DRAFT_ID})
    )
    assert _generate(conn, cfg, adapter, plan) is False
    assert len(adapter.inputs) == MAX_COPY_REFUSALS + 1


def test_build_reports_refused_copy_and_does_not_count_it(conn, tmp_path, monkeypatch):
    _seed_with_sizes(conn, tmp_path, "plan")
    bad = RecordingAdapter(*[_verdict(description="Prints at 16x20.")] * 3)
    monkeypatch.setattr(drafts_mod, "build_copy_adapter", lambda cfg, live: bad)
    report = build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
    assert (report.drafts_built, report.copy_refused, report.pushed) == (0, 1, 0)
    assert "copy refused (1/3)" in report.skip_reasons[0]
    for _ in range(2):  # fill-forward retries; the 3rd refusal hits the cap message
        report = build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
        assert report.drafts_built == 0 and report.copy_refused == 1
    assert "copy refused 3x: needs operator review" in report.skip_reasons[0]
    report = build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
    assert len(bad.inputs) == 3 and report.copy_refused == 1  # 4th run: no LLM call


# --- push / gate3 preflight, fill-forward, frozen plan -------------------------


class _Down(FakeEtsyWriteAdapter):
    def create_draft_listing(self, spec):
        raise OSError("down")


def _build_clean_draft(conn, tmp_path):
    _seed_with_sizes(conn, tmp_path, "plan")
    report = build_drafts(conn, USER_ID, etsy_adapter=_Down())
    assert (report.drafts_built, report.push_failed) == (1, 1)
    return conn.execute("SELECT draft_id FROM proj_listing_drafts").fetchone()[0]


def _stale_copy(conn, draft_id, title="Ridge Wall Art 16x20"):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.copy_generated",
            payload={
                "draft_id": draft_id, "title": title, "tags": ["a"] * 13,
                "description": "Prints up to 16x20.", "materials": None, "model": "m",
                "provider": "p", "disclosure_appended": False,
            },
        ),
    )  # fmt: skip
    rebuild_listings(conn)


def test_push_refuses_stored_copy_naming_undelivered_size(conn, tmp_path):
    _stale_copy(conn, _build_clean_draft(conn, tmp_path))
    adapter = FakeEtsyWriteAdapter()
    report = push_drafts(conn, USER_ID, listing_config.get_config(conn, USER_ID), adapter)
    assert (report.pushed, report.push_failed) == (0, 1)
    assert adapter.calls == []  # zero Etsy calls
    failed = read_all(conn, "listingdraft.push_failed")[-1].payload["error"]["message"]
    assert "copy/panel sizes stale: rebuild with --force" in failed


def test_push_passes_for_clean_built_copy(conn, tmp_path):
    _build_clean_draft(conn, tmp_path)
    report = push_drafts(
        conn, USER_ID, listing_config.get_config(conn, USER_ID), FakeEtsyWriteAdapter()
    )
    assert report.pushed == 1  # the appended Files-included block passes by construction


def test_gate3_retry_refuses_stale_copy_before_create(conn, tmp_path):
    draft_id = _build_clean_draft(conn, tmp_path)
    _stale_copy(conn, draft_id)
    adapter = FakeEtsyWriteAdapter()
    retry_push(conn, USER_ID, draft_id, adapter)
    assert adapter.calls == []
    last = read_all(conn, "listingdraft.push_failed")[-1].payload["error"]["message"]
    assert "sizes stale" in last


class _FailThirdFile(FakeEtsyWriteAdapter):
    def upload_listing_file(self, listing_id, file, *, name, rank):
        if rank == 3:
            raise EtsyWriteError(500, "boom")
        return super().upload_listing_file(listing_id, file, name=name, rank=rank)


def _planned(conn):
    return [e for e in read_all(conn, "listingdraft.files_planned") if e.user_id == USER_ID]


def test_fill_forward_uses_recorded_plan(conn, tmp_path, monkeypatch):
    _seed_with_sizes(conn, tmp_path, "plan")

    class Parse:
        def generate_copy(self, inputs, *, model):
            raise CopyParseError("bad")

    monkeypatch.setattr(drafts_mod, "build_copy_adapter", lambda cfg, live: Parse())
    build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
    assert read_all(conn, "listingdraft.copy_generated") == []  # plan recorded, no copy yet
    good = RecordingAdapter(_verdict())
    monkeypatch.setattr(drafts_mod, "build_copy_adapter", lambda cfg, live: good)
    build_drafts(conn, USER_ID, etsy_adapter=FakeEtsyWriteAdapter())
    assert good.inputs[0].sizes == []
    assert "Files included (5 print-ready JPEG files):" in _description_for_build(conn)
    assert len(_planned(conn)) == 1


def test_force_rebuild_of_frozen_plan_reuses_recorded_plan_for_copy(conn, tmp_path):
    _seed_with_sizes(conn, tmp_path, "plan")
    build_drafts(conn, USER_ID, etsy_adapter=_FailThirdFile())  # listing + files 1-2 exist
    assert len(_planned(conn)) == 1
    build_drafts(conn, USER_ID, force=True, etsy_adapter=FakeEtsyWriteAdapter())
    assert len(_planned(conn)) == 1  # frozen: no second plan
    assert "Files included (5 print-ready JPEG files):" in _description_for_build(conn)


def test_mockups_fallback_to_defaults_when_listing_config_unseeded(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "none"))
    path = tmp_path / "m.tif"
    Image.new("RGB", (6000, 4000), (150, 130, 110)).save(path, format="TIFF")
    _observe(conn, path)
    run_mockups(conn, USER_ID, output_dir=tmp_path / "out")  # listing config never seeded
    row = conn.execute(
        "SELECT params_json FROM proj_mockups WHERE intent='digital_whatyougot'"
    ).fetchone()
    assert "16x20" not in json.loads(row["params_json"])["sizes"]


def test_run_mockups_rerenders_stale_panel_without_force(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPSTEWARD_TEMPLATES_DIR", str(tmp_path / "none"))
    listing_config.seed(conn, USER_ID)
    rebuild_listings(conn)
    path = tmp_path / "m.tif"
    Image.new("RGB", (6000, 4000), (150, 130, 110)).save(path, format="TIFF")
    _observe(conn, path)
    assert run_mockups(conn, USER_ID, output_dir=tmp_path / "out").sets_completed == 1
    again = run_mockups(conn, USER_ID, output_dir=tmp_path / "out")
    assert (again.sets_completed, again.skipped_idempotent) == (0, 1)  # fresh panel: skipped
    listing_config.set_value(conn, USER_ID, "digital_files.ppi", 200)  # plan sizes change
    assert run_mockups(conn, USER_ID, output_dir=tmp_path / "out").sets_completed == 1
    row = conn.execute(
        "SELECT params_json FROM proj_mockups WHERE intent='digital_whatyougot'"
    ).fetchone()
    assert "16x20" in json.loads(row["params_json"])["sizes"]


def _observe(conn, path):
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": "a" * 64, "path": str(path), "base_name": None, "format": "TIFF",
                "width": 6000, "height": 4000, "color_space": "RGB", "photo_id": "p",
            },
        ),
    )  # fmt: skip
    rebuild_pipeline(conn)


class _Unparseable:
    def __init__(self) -> None:
        self.calls = 0

    def generate_copy(self, inputs, *, model):
        self.calls += 1
        usage = CopyUsage(model=model, input_tokens=10, output_tokens=5, est_cost_usd=0.01)
        raise CopyParseError("junk", usage=usage)


def test_unparseable_billed_response_is_recorded_and_capped(conn, cfg):
    plan = plan_files((6000, 4000), None, cfg.digital_files)
    adapter = _Unparseable()
    for _ in range(MAX_COPY_REFUSALS):
        assert _generate(conn, cfg, adapter, plan) is False
    assert len(read_all(conn, "llm.call")) == MAX_COPY_REFUSALS
    refused = read_all(conn, "copy.refused")
    assert [e.payload["reason"] for e in refused] == ["unparseable"] * MAX_COPY_REFUSALS
    assert _generate(conn, cfg, adapter, plan) is False
    assert adapter.calls == MAX_COPY_REFUSALS  # 4th run made no adapter call


def test_unparseable_without_usage_records_no_llm_call(conn, cfg):
    class NoUsage:
        def generate_copy(self, inputs, *, model):
            raise CopyParseError("junk")

    assert _generate(conn, cfg, NoUsage(), None) is False
    assert read_all(conn, "llm.call") == []
    assert len(read_all(conn, "copy.refused")) == 1
