# Design: autonomous `listing.gapfill_reprint` (AUTO, 1/week, physical read-back gate)

Status: proposal 2026-11-27 (architect sub-agent; saved by the orchestrator).
The operator selected "Full chain, 1/week" on 2026-11-27; the CLAUDE.md
amendment lands with the implementation PR.

PRD discrepancy: PRD §10 still marks M5 (Gelato listing + enrichment)
superseded, but the POD path is live in code (#53-#55). This design relies on it.

## 1. Where money is actually spent

- **Gelato create costs nothing up front.** `adapters/pod/live.py::create_product`
  POSTs create-from-template with no price or payment; base cost exists only in
  `pod.json` as a pricing input and is booked per sale as fulfillment COGS
  (buyer-funded), not capital at risk before a sale.
- **Etsy draft is free; the $0.20 fee is at publish.** `governor.month_spend_parts`
  already counts one `catalog_expansion.listing_fee_usd` per `gate3.published`, and
  POD publishes go through `gate3.publish`.
- **Cap charge:** `estimated_cost_usd` = the $0.20 listing fee, so the governor's
  BUDGET check reserves it before any write; `ExecutionResult.cost_usd = 0` (no
  double count). The governor's budget line takes
  `max(action.estimated_cost_usd, cap.estimate_cost_usd(action))`, because stored
  proposals carry 0.0.
- **Hidden recurring fee:** a listing with `should_auto_renew=True` renews every
  4 months, which the cap can't see. The chain sets auto-renew off before publish;
  `listing.autorenew_on` turns it on only after an in-window sale.
- **Per-sale renewal** of a multi-quantity item ($0.20) comes out of the sale:
  contribution profit, not the cap.
- **CLAUDE.md premise is wrong:** "POD SKU base cost at Gate-3-approval time" and
  the runbook's "creates a paid POD SKU" contradict the code. Reserving base cost
  would also make acrylic ($71) permanently BUDGET-refused.

## 2. The chain at AUTO (reuses existing steps; mirrors catalog_expand)

Steps 0-4 retryable (a raise = non-terminal refusal, counts as an attempt);
after step 5 nothing raises.

0. **Re-validate:** the target is still in `_candidates()`, else `StaleTargetError`.
1. **Local preflight, no network:** `printfile.resolve_print_source_path` resolves
   (with archive fallback); the mockup set exists or
   `mockup_hook("run_mockups")(photo_id, file_id)` builds it. An adopted photo whose
   landing file is gone fails here, before any provider write, and goes to
   NEEDS YOU after the attempt cap.
2. **Build:** `build_pod_reprint(..., print_file_host=<live R2 host>)`; on
   `already_exists`, resume with that `draft_id`.
3. **Link:** `link_pod_drafts(..., draft_ids={draft_id})` (**new filter**; today it
   links every unlinked POD draft). Not linked: raise, re-poll next run. The stored
   `provider_product_id` is reused, never re-created.
4. **Enrich:** `enrich_pod_drafts(..., draft_ids={draft_id})` (**new filter**), copy
   with `medium="physical"`. **Fix:** drop the `digital_whatyougot` intent for POD
   drafts (it would put a "5 digital files" panel on a canvas listing). Not
   enriched: raise.
5. **Auto-renew off:** if the read-back shows `should_auto_renew`,
   `update_listing(EtsyListingUpdate(should_auto_renew=False))` (a listing field,
   not a SKU or variation).
6. **Read-back gate:** `run_physical_readback` pass, then `gate3.publish` (still the
   only publish call site). Fail: stays a draft under NEEDS YOU. Either way the
   action is `executed` with
   `after={draft_id, etsy_listing_id, provider_product_id, published, problems}`.

`run_readback` is digital-only (5 files, 7 images, "(Digital Download)", landing
path). Add `run_physical_readback` in the same file, with the same
`listingdraft.readback` event and fail-closed try. All reads:

- state is `draft`; 0 digital files (`list_listing_files`); the title lacks
  "Digital Download" and contains `pod.copy.title_suffix[product_type]`; 13 tags;
  1..`pod.images.hard_cap` images; no `digital_whatyougot` in local `images_json`;
- `production_partners` non-empty (additive optional field on `EtsyListingDetail`);
- **price (load-bearing):** `get_listing_inventory` via the read adapter. Product
  count equals `len(variants_json)` (Gelato's variant structure intact), and every
  offering passes `pod.pricing.enforce_floor` against the draft's maximum variant
  unit cost (ponytail: conservative, so a size-mapping mismatch can't pass a
  below-floor price). Gelato's template sets the price and we never write it, so a
  below-floor price fails the gate; it is never "fixed";
- `should_auto_renew` is False;
- vision copy check (`copy_check_model`) against the photo via
  `resolve_print_source_path`, so it works when the landing file is gone.

## 3. Undo

- If `after.published`: `update_listing_state(listing_id, "inactive")`. The $0.20 is
  not refunded. This is a working undo (registry invariant 1).
- The Gelato product is left in place: `delete_product` removes the connected
  Etsy listing (M5b cleanup notes), which would make undo irreversible and lose
  listing history. An idle Gelato product costs nothing; `provider_product_id`
  stays in `after` for manual cleanup.
- The R2 print file expires on its own (`host_ttl_seconds` = 24 h).
- A gate-failed draft has nothing to undo (Etsy drafts are free).

## 4. Pace and cap

- **Config:** `gapfill: {max_per_week: 1, max_attempts: 3}` in `ops.json` and
  `_OpsGapfill` (default factory) in `models.py`. Never in `pod.json`:
  `pod_config_hash` is part of `draft_id`, so a change there re-keys every draft.
  Tier floor `"listing.gapfill_reprint": 0`.
- **Pace unit = one new Gelato product, not one execution** (so a failure after
  create can't let a second product through the same week). The governor refuses
  PACE for an action whose draft has no `listingdraft.provider_create_attempted`
  when this ISO week already has `max_per_week` first-attempts on gapfill drafts, or
  another gapfill draft is in flight. Resuming an in-flight draft is never
  pace-refused. `draft_id` goes into `params` at propose time; older proposals
  recompute it from `photo_id`, `product_type` and the config hash.
- Add gapfill to `_PORTFOLIO_CAP_EXEMPT` (it only adds listings).
- **Operator approve:** `approve_action` calls the same `govern()`, so PACE
  throttles the operator today. Proposal: `govern(..., operator=True)` skips only
  the weekly listing-creation paces (gapfill and catalog_expand). HALT, EXPIRED,
  BUDGET and PRECONDITION still apply, and an operator create still uses up that
  week's agent slot.
- **The 3 pending Grand Teton proposals** (expire 12-07): once AUTO,
  `_stale_pending` drives them through `govern()`. At most one per ISO week, the
  rest get non-terminal PACE refusals. Monday runs: one on 11-30 (W49), one on 12-07
  (W50; expiry is `today > expires_at`). The third expires and is re-minted by
  `propose()` while still eligible, about 12-14.

## 5. Live gates

`precondition_ok` requires: `live_gelato_open()` (`SHOPSTEWARD_LIVE_GELATO`,
`GELATO_API_KEY`, `GELATO_STORE_ID`), `live_printfile_open()`
(`SHOPSTEWARD_LIVE_PRINTFILE` plus the 4 R2 vars), `live_copy_open()`, the live
copy checker (openrouter plus `live_vision_open`), and `live_etsy_read_open()`
(inventory read).

- Add the capability to `runner.LIVE_GATED_CAPABILITIES`.
- **`scripts/ops.sh` lacks `SHOPSTEWARD_LIVE_GELATO=1` and
  `SHOPSTEWARD_LIVE_PRINTFILE=1`; add both.**
- Both registration sites in `ops/cli.py` hardcode `build_print_file_host(live=False)`;
  replace them with a `_gapfill(adapter)` helper modelled on `_catalog_expand`.

## 6. Failure modes

- **POD created, Etsy step failed:** `provider_product_id` is persisted by
  `provider_created`; the retry re-polls, then enriches (`enrich_failed` is already
  retryable). Never re-creates.
- **Ambiguous create:** append `listingdraft.provider_create_attempted {draft_id}`
  before `create_product`. Live create sends no idempotency key, and httpx timeouts
  aren't caught today: map `httpx.TransportError` to `PodWriteError(0, ...)` in
  `live.py`. Attempted but unconfirmed: fail closed. Never re-create, drop the
  target from `_candidates()`, and list it under NEEDS YOU as "check the Gelato
  dashboard for draft X".
- **Idempotency:** `draft_id` is deterministic; `build_pod_reprint` returns
  `already_exists`; link and enrich skip finished stages; `publish` checks
  eligibility.
- **Finished** (the `_finished_file_ids` model): published, gate-failed, or
  `max_attempts` failures (`action.failed` / `execute_revalidation_error`). An
  unfinished existing draft stays a candidate; older offline-built reprint drafts
  are resumed at link, under the same pace.

## 7. Files, build order, tests

1. `pod/provider.py`: `draft_ids` filter and the attempted event.
2. `adapters/pod/live.py`: transport-error mapping.
3. `pod/enrich.py`: `draft_ids` filter and the physical image filter.
4. `listings/readback.py` (`run_physical_readback`) and `etsy/models.py`
   (`production_partners`).
5. `ops/models.py` and `ops.json`.
6. `governor.py`: gapfill pace, operator flag, exemption, budget max.
7. `runner.py`: live-gated set and `operator=True`.
8. `capabilities/gapfill.py`: chain, `max_tier=AUTO`, undo, `precondition_ok`,
   candidates.
9. `ops/cli.py` and `scripts/ops.sh`.
10. Docs: the CLAUDE.md amendment, the runbook "Hard limits" line, new policy row
    E17 ("physical POD listing of our own photo; production partner declared"),
    and `policy_verified` set True only in the PR that adds E17.

Tests (fakes only), `test_gapfill_auto.py`, end to end on FakeGelato and
FakeEtsyWrite:

- one proven seller gives exactly 1 provider create, 1 `gate3.published`, and
  $0.20 month spend;
- a second target the same week is PACE-refused, and the operator approving it
  executes it;
- an unrelated pre-existing POD draft is untouched (fan-out);
- a link failure then a retry gives exactly one create;
- an attempted-but-unconfirmed create is never re-created;
- a below-floor inventory price or a `digital_whatyougot` image fails the gate and
  the draft stays a draft;
- undo deactivates the listing and never calls `delete_product`;
- `test_tiers.py` updated.

## 8. Rejected alternatives, guardrails, rollback

- **Rejected:** calling `run_shop_build` (folder scan plus fan-out over every POD
  draft, can't be paced); reserving base cost against the cap (Gelato bills per
  order, and $71 exceeds the $20 cap); undo via `delete_product` (irreversible,
  loses listing history); keeping PROPOSE plus `shop build` (contradicts the
  authorization and stays unpaced).
- **Guardrails:** POD-first holds (provider SKUs, variations and price are read,
  never written; the only write after enrich is `should_auto_renew`); AI only
  checks copy; every new fact is an event; every new field carries `user_id`.
- **Rollback:** remove the tier floor entry, or unset `SHOPSTEWARD_LIVE_GELATO`
  (`precondition_ok` goes False and the governor refuses). `ops halt` on any of:
  more than one create in a week, spend above $0.20 per execution, a published
  inventory that differs from Gelato's, or wrong copy after publish.

## 9. C-Suite critique (folded in)

- **CTO:** the fan-out in `link_pod_drafts` is the real bug: without a `draft_ids`
  filter the first AUTO run could create a Gelato product for every old draft.
  Make the filter required from the capability, and test that unrelated drafts are
  untouched (§7).
- **CFO:** the cap assumed base cost at approval, but real exposure is per-order
  base cost against a template price we can't edit. Read-back floor check against
  the maximum variant cost (§2) and auto-renew off, so no fee is invisible to the
  cap (§1).
- **CMO:** a canvas listing with a "digital files" panel or "Digital Download"
  wording confuses buyers on the best photo. Image-intent filter in enrich and
  negative title checks in the read-back (§2). Order candidates by units sold.
- **CPO:** a fail-closed gate that silently leaves drafts is worse than PROPOSE.
  Each failure (gate, ambiguous create, attempt cap) gets one NEEDS-YOU line with
  the draft id and exact problem, reusing the brief's `listingdraft.readback`
  reader (§6).
- **Chief Legal:** Etsy requires a production-partner disclosure for POD items.
  M5b made it precondition #14, but no code checks it. Read-back check that
  `production_partners` is non-empty, and `policy_verified` stays False until
  policy row E17 exists (§2, §7).

## Open operator questions

1. Does the Gelato account have any per-product fee or required subscription?
   The repo has no billing evidence either way.
2. Is the shop's production-partner (Gelato) declaration in place on Etsy?
