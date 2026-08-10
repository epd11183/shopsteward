# Design: Winners-folder shop-building — Phase C1+C2 (POD provider link + enrichment, fakes)

**Date:** 2026-08-10
**Status:** Approved (design). Builds slices 3–4 against the **fake** Gelato +
Etsy adapters. The **live Gelato HTTP adapter** and its smoke are deferred to
**C3** (blocked only on real Gelato template/store/variant IDs, which the
operator has set up).
**Effort #2, Phase C (parts 1–2 of 3).**

## Background

Phase B leaves each winner with a POD draft at `listingdraft.print_file_hosted`
(variants selected + priced + print file hosted). The POD provider contract is
fully modeled and a complete **fake** Gelato adapter exists:

- `PodAdapter` (`adapters/pod/interface.py`): `create_product(spec)` creates the
  Gelato product **and** the async connected-store Etsy **draft**; `get_product`
  polls until `status="linked"` with an `etsy_listing_id`; idempotent by
  `spec.idempotency_key`; `publish_as_draft=True` write-safety.
- `PodProductSpec` (ref: provider=gelato, `store_id`, `template_id`,
  `variants[PodVariantSpec]`; `title`/`description`/`tags`; `print_file_url`
  (transient — never persisted/logged); `idempotency_key`). `PodProduct`
  (provider_product_id, status, etsy_listing_id, etsy_listing_state, ...).
- `FakeGelatoAdapter` simulates create→poll→link and enforces
  template_id + per-variant placeholder + idempotency.
- Digital `listings/push.py` shows the Etsy enrichment machinery
  (`EtsyWriteAdapter`, `update_listing`, image upload) this phase reuses.

## Scope (C1 + C2, all against fakes)

### C1 — Provider create → poll → link (slice 3)
**`pipeline/listings/pod/factory.py::build_pod_adapter(*, live: bool) -> PodAdapter`**
(new; fake unless live — the live branch raises `NotImplementedError("live Gelato
adapter: Phase C3")` for now, so nothing can accidentally go live yet).

**`pipeline/listings/pod/provider.py`** (new) — `link_pod_drafts(conn, user_id,
*, adapter, poll_max=..., force=False)`: for each POD draft at
`print_file_hosted` with no confirmed `provider_product_id`:
- Build `PodProductSpec` from the draft's selected+priced variants, the hosted
  `print_file_url`, the copy (title/tags/description — see C2 ordering note), and
  the Gelato mapping from config (`store_id`, `template_id`, per-format
  `variant_key`/`placeholder`/`fit_method`). `idempotency_key = draft_id`.
- `create_product(spec)` → emit `listingdraft.provider_created`
  (provider_product_id; **never** the print_file_url).
- Poll `get_product` up to `poll_max` (config) until `status="linked"` → emit
  `listingdraft.provider_linked` (etsy_listing_id, etsy_listing_state="draft");
  on `status="failed"` or poll exhaustion → emit `listingdraft.provider_failed`
  with the reason (no raise; other drafts continue).
- **Idempotent:** a draft with a confirmed `provider_product_id` is never
  re-created (design §3).

### C2 — Enrichment (slice 4)
Physical copy + images onto the Gelato-linked Etsy draft (price is already set at
Gelato create — Etsy `updateListing` has no price field).

**`pipeline/listings/pod/enrich.py`** (new) — `enrich_pod_drafts(conn, user_id,
*, etsy_adapter, copy_adapter, ...)`: for each POD draft that is `provider_linked`
(has `etsy_listing_id`, state `draft`) and not yet enriched:
- **Copy:** generate title/tags/description via the copy adapter, reusing the
  Phase-A vision signals (pass the synthetic `file-<file_id[:12]>` photo_id so
  `proj_scores` is read — same threading as digital `build_drafts`).
- **Images:** upload the POD product images (the hosted mockups/print preview)
  in rank order via the Etsy write adapter.
- `update_listing(etsy_listing_id, EtsyListingUpdate(title, description, tags,
  ...))`; emit `listingdraft.enriched`. Idempotent per stage (mirror
  `push.py`'s stage gating so a partial failure can resume).
- Gated by `live_etsy_write` (reuse `build_etsy_write_adapter(live=...)` +
  `live_etsy_write_open`); fake by default.

### Config
Add a `gelato` mapping to `config/defaults/pod.json`: `store_id`, `template_id`,
and per-format `variant_key`/`placeholder`/`fit_method`, plus `poll_max` /
`poll_interval`. **Placeholder values now** (clearly marked); the operator's real
Gelato template/store/variant IDs are filled at C3 before any live smoke. The
fake adapter only requires template_id present + a placeholder per variant, so
the fakes exercise the full flow with placeholder config.

### Orchestration
Extend `run_shop_build` / `shop build`: after `build_pod_drafts`, run
`link_pod_drafts` (fake adapter unless a new `--live-gelato` flag, which for now
refuses with the C3 not-implemented message) then `enrich_pod_drafts` (Etsy fake
unless `--live-etsy-write`). Summary gains provider-linked / enriched counts.

## Data flow (this phase in bold)
```
shop build <folder>
 → ... → build_pod_drafts            (print_file_hosted, Phase B)
 → link_pod_drafts   (fake Gelato)   ← C1: create -> poll -> provider_linked (etsy draft)
 → enrich_pod_drafts (fake Etsy)     ← C2: copy + images onto the linked draft -> enriched
 → [operator] Gate 3 review/publish
```

## Error handling
- `create_product`/poll failure → `listingdraft.provider_failed` (reason),
  non-fatal; other drafts proceed; digital listings unaffected.
- Enrichment stage failure → recorded per stage, resumable (push.py precedent);
  no duplicate uploads on re-run.
- `--live-gelato` set → refuse with "live Gelato adapter is Phase C3" until C3.
- `--live-etsy-write` set but gate closed → refuse up front.
- Idempotency: confirmed provider_product_id never re-created; enrich stages gated
  by what already landed.
- `print_file_url` is transient — never written to an event or logged.

## Testing (fixtures only — no live APIs)
- C1: with `FakeGelatoAdapter`, a hosted POD draft goes create→poll→linked and
  records `provider_created` + `provider_linked` with an etsy_listing_id;
  idempotent (second run creates nothing new); a fake that never links →
  `provider_failed` after poll_max, other drafts unaffected.
- C2: a linked draft gets copy (carrying the vision subject signal via the
  synthetic id) + images via `FakeEtsyWriteAdapter`; `update_listing` called with
  the expected title/tags; stage-resumable; idempotent.
- Orchestration: `shop build` (all fakes) drives a winner from folder → digital
  draft + physical draft that reaches `enriched`; summary counts correct.
- `--live-gelato` refusal message points at C3.

## Operator-review items (per CLAUDE.md)
1. **C3 prerequisite:** real Gelato `store_id`/`template_id`/variant IDs +
   Gelato API creds — needed before C3's live adapter + smoke, not this phase.
2. **Etsy write** live smoke (`--live-etsy-write`) stays operator-gated.

## Out of scope
- **C3:** the live Gelato HTTP adapter (`create_product`/`get_product` over
  Gelato's real API) + its smoke.
- Instagram promotion + performance feedback loop.
- Additional POD providers (Gelato-only).
