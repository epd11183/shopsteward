# Winners-folder Shop-building Phase C1+C2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Drive each Phase-B POD draft (at `print_file_hosted`) through provider create→poll→link (slice 3) and copy/image enrichment onto the linked Etsy draft (slice 4), **all against fakes**. The live Gelato HTTP adapter is C3.

**Architecture:** New code in `pipeline/listings/pod/` (may import `adapters/pod`, `adapters/etsy`). Orchestration wired from top-level `shop.py`. Fakes by default; `--live-gelato` refuses (C3 not built); `--live-etsy-write` gated as today.

**Spec:** `docs/superpowers/specs/2026-08-10-winners-folder-shop-phase-c-design.md`

## Verified facts
- `PodAdapter` (`adapters/pod/interface.py`): `create_product(spec: PodProductSpec) -> PodProduct` (makes Gelato product + async Etsy draft; idempotent by `spec.idempotency_key`), `get_product(id) -> PodProduct` (poll until `status="linked"`, sets `etsy_listing_id`), `delete_product`. `FakeGelatoAdapter(links_after_polls=2)` implements all three.
- `PodProductSpec`: `ref: PodProviderRef(provider="gelato", store_id, template_id, variants:[PodVariantSpec(format, variant_key, placeholder, fit_method, retail_price)])`, `title`, `description`, `tags`, `print_file_url` (transient — NEVER persisted/logged), `idempotency_key`, `publish_as_draft=True`. `PodProduct`: `provider_product_id, status(created|publishing|linked|failed), etsy_listing_id, etsy_listing_state, variant_count, error`.
- Phase B's `build_pod_drafts` emits `listingdraft.created/variants_selected/priced/print_file_prepared/print_file_hosted`. `printfile.py` has `resolve_print_source_path`, `prepare_print_file`, `publish_print_file` (the last returns a hosted URL). `factory.build_print_file_host(*, live)`.
- Digital `listings/push.py` shows Etsy enrichment: `build_etsy_write_adapter(*, live)`, `EtsyWriteAdapter.update_listing(...)` + image upload, staged + idempotent by what already landed.
- `copy.generate_copy(conn, user_id, draft_id, landing_file_id, photo_id, images, adapter, cfg, *, live, soft_cap_usd)` + `build_copy_adapter`. Phase A threads `photo_id = row["photo_id"] or f"file-{file_id[:12]}"` so vision signals in `proj_scores` are read.
- `run_shop_build` (top-level `shop.py`) currently ends at `build_pod_drafts`.

---

## Task 1: Gelato config + `build_pod_adapter` factory

**Files:** Modify `config/defaults/pod.json`, `src/shopsteward/pipeline/listings/pod/config.py`, `src/shopsteward/pipeline/listings/pod/factory.py`; Test `tests/pipeline/listings/pod/test_pod_adapter_factory.py`

- [ ] **Step 1:** Read `pod/config.py` (its `PodConfig` model) + `pod/models.py` (`PodVariantSpec`/`PodProviderRef`) to see how to represent the Gelato mapping. Add a `gelato` block to `config/defaults/pod.json`: `store_id`, `template_id`, `poll_max` (e.g. 10), `poll_interval_seconds` (e.g. 0 for tests/fakes), and a per-format `variants` map (`format -> {variant_key, placeholder, fit_method}`). Use **clearly-marked placeholder IDs** (e.g. `"template_id": "REPLACE_AT_C3"`) — the fake only needs them present. Extend `PodConfig` (config.py) to load the block.
- [ ] **Step 2:** Write `tests/pipeline/listings/pod/test_pod_adapter_factory.py`: `build_pod_adapter(live=False)` returns a `FakeGelatoAdapter`; `build_pod_adapter(live=True)` raises `NotImplementedError` mentioning "C3". Run → FAIL.
- [ ] **Step 3:** Add `build_pod_adapter(*, live: bool) -> PodAdapter` to `pod/factory.py`:
```python
def build_pod_adapter(*, live: bool):
    from shopsteward.adapters.pod.fake import FakeGelatoAdapter
    if not live:
        return FakeGelatoAdapter()
    raise NotImplementedError("live Gelato adapter is Phase C3 (not yet built)")
```
- [ ] **Step 4:** Run → pass. `ruff`. Commit `feat(pod): gelato config mapping + build_pod_adapter factory (fake; live=C3)`.

---

## Task 2: Provider create → poll → link (C1)

**Files:** Create `src/shopsteward/pipeline/listings/pod/provider.py`; Test `tests/pipeline/listings/pod/test_provider_link.py`

- [ ] **Step 1 (discovery):** Read `pod/build.py` + `pod/projections.py` to find, per POD draft: `draft_id`, `landing_file_id`, the **selected+priced variants** (format, variant_key?, retail_price) and how they're stored/queried (a projection table or the `variants_selected`/`priced` events). Confirm the print-file URL is NOT persisted (spec) — so link must **re-host** to get a fresh URL: reuse `resolve_print_source_path` → `prepare_print_file` → `publish_print_file(host, ...)` with a host from `build_print_file_host(live=live_printfile)`. Note the exact signatures.
- [ ] **Step 2:** Write `tests/pipeline/listings/pod/test_provider_link.py` with `FakeGelatoAdapter`: seed a POD draft that reached `print_file_hosted` (reuse Phase B test helpers), run `link_pod_drafts(conn, USER, adapter=FakeGelatoAdapter(), print_file_host=<fake>, cfg=...)`, assert `listingdraft.provider_created` then `listingdraft.provider_linked` (with `etsy_listing_id`) are emitted and no `print_file_url` appears in any event payload. Second test: idempotent (second run creates nothing — assert adapter.calls has no new create). Third: `FakeGelatoAdapter(links_after_polls=999)` with a small `poll_max` → `listingdraft.provider_failed`, no raise. Run → FAIL.
- [ ] **Step 3:** Implement `link_pod_drafts(conn, user_id, *, adapter, print_file_host, cfg, poll_max=None, force=False)`:
  - Select POD drafts at `print_file_hosted` without a confirmed `provider_product_id` (from events/projection).
  - For each: build `PodVariantSpec[]` from the draft's selected variants + the config gelato per-format mapping (variant_key/placeholder/fit_method) + retail_price; build `PodProviderRef` (store_id/template_id from config); re-host the print file (resolve→prepare→publish) to get a fresh `print_file_url`; assemble `PodProductSpec(idempotency_key=draft_id, publish_as_draft=True, ...)` with copy title/desc/tags (use a minimal placeholder title/desc here if copy isn't generated until enrich — Gelato requires non-empty title/description; Step: use the draft's existing copy if present, else a safe generated-at-enrich placeholder that enrich overwrites).
  - `create_product` → emit `listingdraft.provider_created` (provider_product_id, variant_count — NOT the url). Poll `get_product` up to `poll_max` (config) → on linked emit `listingdraft.provider_linked` (etsy_listing_id, state); on failed/exhausted emit `listingdraft.provider_failed` (reason). Never raise on a single draft's failure.
  - Idempotent: skip drafts with a confirmed provider_product_id unless `force`.
  - `rebuild_pipeline` / return a small report (created/linked/failed counts).
- [ ] **Step 4:** Run → pass. `ruff`, `lint-imports`. Commit `feat(pod): provider create->poll->link over hosted POD drafts (C1)`.

Note on copy/title at create: Gelato needs a title/description at create. If the built copy isn't available pre-enrich, pass a minimal deterministic placeholder (e.g. the event name / base_name) at create and let C2 enrichment overwrite title/description on the Etsy draft. Confirm `PodProductSpec` validation (title/description min_length=1) is satisfied.

---

## Task 3: Enrichment — copy + images onto the linked draft (C2)

**Files:** Create `src/shopsteward/pipeline/listings/pod/enrich.py`; Test `tests/pipeline/listings/pod/test_enrich.py`

- [ ] **Step 1 (discovery):** Read `listings/push.py` for the exact `EtsyWriteAdapter.update_listing` + image-upload calls and the stage-idempotency pattern; read `copy.generate_copy` + `build_copy_adapter` and how digital drafts store generated copy; find the POD product images to upload (the hosted mockups / print preview — where they're recorded).
- [ ] **Step 2:** Write `tests/pipeline/listings/pod/test_enrich.py` with `FakeEtsyWriteAdapter` + `FixtureCopyAdapter`: seed a `provider_linked` POD draft (has etsy_listing_id) whose winner has a `photo.scored` vision row under `file-<id>` (subject="trail runner"); run `enrich_pod_drafts(conn, USER, etsy_adapter=..., copy_adapter=..., cfg=...)`; assert `update_listing` was called with a title/description reflecting the subject signal, images uploaded, and a `listingdraft.enriched` event emitted. Second test: idempotent / stage-resumable (re-run doesn't re-upload). Run → FAIL.
- [ ] **Step 3:** Implement `enrich_pod_drafts(conn, user_id, *, etsy_adapter, copy_adapter, cfg, live=False, soft_cap_usd=...)`:
  - Select `provider_linked` POD drafts (etsy_listing_id present, state draft) not yet `enriched`.
  - Generate copy via `generate_copy(... photo_id = row["photo_id"] or f"file-{file_id[:12]}" ...)` so vision signals are read (same threading as digital).
  - Upload POD images in rank order + `update_listing(etsy_listing_id, EtsyListingUpdate(title, description, tags, ...))` (price already set at Gelato create). Stage-gate like push.py; emit `listingdraft.enriched`.
- [ ] **Step 4:** Run → pass. `ruff`, `lint-imports`. Commit `feat(pod): enrich linked Etsy draft with copy + images (C2)`.

---

## Task 4: Wire link + enrich into `shop build`

**Files:** Modify `src/shopsteward/shop.py`, `src/shopsteward/cli.py`; Test `tests/pipeline/test_shop_build_physical.py`

- [ ] **Step 1:** In `run_shop_build`, after `build_pod_drafts`, add: `link_pod_drafts(... adapter=build_pod_adapter(live=live_gelato), print_file_host=build_print_file_host(live=live_printfile), cfg=...)` then `enrich_pod_drafts(... etsy_adapter=build_etsy_write_adapter(live=live_etsy_write), copy_adapter=build_copy_adapter(live=live_copy), ...)`. Add `live_gelato: bool = False` param; if set, `build_pod_adapter(live=True)` raises the C3 NotImplementedError — catch at CLI for a clean message, or gate: refuse up front with "live Gelato is Phase C3". Extend the summary with provider-linked / enriched counts.
- [ ] **Step 2:** Add `--live-gelato` to `shop build` (default False), thread through. Keep existing flags.
- [ ] **Step 3:** Write `tests/pipeline/test_shop_build_physical.py`: fixture winner through `run_shop_build(...)` (all fakes) now reaches a POD draft that is `enriched`; summary counts include linked/enriched. Run → pass.
- [ ] **Step 4:** `ruff`, `lint-imports`. Commit `feat(shop): drive POD drafts through provider link + enrich in shop build`.

---

## Task 5: Full gate + PR

- [ ] `uv run pytest -q` (green), `uv run ruff check src tests` (clean), `uv run lint-imports` (3 kept), `uv run shopsteward shop build --help` (shows `--live-gelato`).
- [ ] Push + PR against `main`. Note **C3** (live Gelato HTTP adapter + smoke) is the remaining follow-on, and the operator's real Gelato `store_id`/`template_id`/variant IDs must replace the `pod.json` placeholders before C3's smoke.

---

## Self-review (against the spec)
- C1 create→poll→link with fake + idempotency + failure path (Task 2); transient print_file_url re-hosted at link, never persisted (Task 2 discovery). C2 enrich copy (vision signals via synthetic id) + images + update_listing, staged/idempotent (Task 3). Factory gates live at C3 (Task 1). Orchestration + `--live-gelato` refusal (Task 4). Fakes-only tests; live Etsy stays gated. Import boundary checked (Tasks 2–5).
- Discovery directives (variant-selection storage; print_file re-host signatures; push.py enrich calls; POD image source) are explicit with a concrete verification (a passing test asserting the emitted events / update_listing args), not open placeholders.
