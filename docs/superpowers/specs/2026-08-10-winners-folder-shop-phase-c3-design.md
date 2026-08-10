# Design: Winners-folder shop-building — Phase C3 (live Gelato adapter)

**Date:** 2026-08-10
**Status:** Approved (design). Adapter + respx tests build now; the **live smoke**
is operator-gated (needs real Gelato API key + `pod.json` IDs).
**Effort #2, Phase C part 3 of 3 — the final piece.**

## Background

C1+C2 drive POD drafts through create→poll→link + enrichment against the **fake**
Gelato adapter. `build_pod_adapter(live=True)` currently raises
`NotImplementedError("...C3...")`. This phase implements the **live** Gelato
adapter satisfying the `PodAdapter` protocol (`create_product`/`get_product`/
`delete_product`) the fake already models, and wires the factory. httpx only, no
vendor SDK (OpenRouter/Etsy live-adapter precedent).

## Gelato Ecommerce API (confirmed via docs)

- Base: `https://ecommerce.gelatoapis.com`. Auth header: `X-API-KEY`.
- **Create:** `POST /v1/stores/{storeId}/products:create-from-template` — body:
  `templateId`, `title`, `description`, `isVisibleInTheOnlineStore`,
  `salesChannels` (`["web"]`), `tags`, `variants[{templateVariantId,
  imagePlaceholders:[{name, fileUrl, fitMethod}]}]`, `productType`, `vendor`.
  Response: `{id, storeId, externalId, status, ...}` — `status ∈ {created,
  publishing, active, publishing_error}`; `externalId` = the connected store's
  (Etsy) listing id, `null` until linked.
- **Get:** `GET /v1/stores/{storeId}/products/{productId}` — same shape (poll it).
- **Delete:** `DELETE /v1/stores/{storeId}/products/{productId}` — smoke cleanup.

## Adapter (`src/shopsteward/adapters/pod/live.py`)

`LiveGelatoAdapter(api_key, store_id, base=..., timeout=...)` implementing
`PodAdapter`. httpx.Client with `X-API-KEY`. Maps our provider-agnostic spec to
Gelato and Gelato's response back to the normalised `PodProduct`.

### create_product(spec) → PodProduct
- URL: `{base}/v1/stores/{spec.ref.store_id}/products:create-from-template`.
- Body: `templateId = spec.ref.template_id`; `title`/`description`/`tags` from
  spec; `variants` from `spec.ref.variants` →
  `{templateVariantId: v.variant_key, imagePlaceholders: [{name: v.placeholder,
  fileUrl: spec.print_file_url, fitMethod: v.fit_method}]}`.
- **Write-safety (pinned):** `isVisibleInTheOnlineStore = False` — the POD twin
  of decision 41; this is what makes the Gelato-created **Etsy listing a draft,
  never live**. `spec.publish_as_draft` is `Literal[True]`, so the adapter always
  sends `False` here; there is no code path that publishes a live listing.
- Idempotency: the caller (`link_pod_drafts`) never re-creates a confirmed
  product; the adapter additionally sends no client-side key (Gelato has no
  documented idempotency header here — the caller's `provider_product_id` guard
  is the dedupe). If Gelato returns a create error, raise `PodWriteError`.
- Response → `PodProduct`: `provider_product_id = id`, `status = _normalise(...)`,
  `etsy_listing_id = _as_int(externalId)`, `etsy_listing_state = "draft" if
  linked else None`, `variant_count = len(spec.ref.variants)`.

### get_product(provider_product_id) → PodProduct
- `GET .../products/{provider_product_id}`; map the same way (poll target).

### delete_product(provider_product_id) → None
- `DELETE .../products/{provider_product_id}`; raise `PodWriteError` on non-2xx.

### Status normalisation (`_normalise`)
Gelato → our `PodProduct.status`:
- `created` → `created`; `publishing` → `publishing`;
- `active` → `linked` **iff** `externalId` is present (the Etsy listing exists);
  `active` with no `externalId` → `publishing` (still linking);
- `publishing_error` (or any unknown) → `failed` (carry the message in `error`).
`etsy_listing_state` is `"draft"` whenever we report `linked` (we created it
non-visible); never `"active"`.

### Errors
Non-2xx → `PodWriteError(status_code, provider_message)` (never the raw body;
truncated, per the existing error class). Network/timeout → `PodWriteError` with
a synthesised status (e.g. 0) + message.

## Factory wiring (`pod/factory.py`)
`build_pod_adapter(*, live)`: when `live`, construct `LiveGelatoAdapter` from the
API key (env, e.g. `GELATO_API_KEY`) + `store_id` (from `cfg.gelato.store_id` /
config), instead of raising. Add a live gate `live_gelato_open()` /
`live_gelato_error()` in `pipeline/live_gate.py` (`SHOPSTEWARD_LIVE_GELATO=1` +
`GELATO_API_KEY`), and have `run_shop_build`'s `--live-gelato` refusal use it
(replacing the "always refuse" stub). The factory needs the key+store_id; pass
them in (the CLI/orchestration reads env + config and constructs the adapter, or
passes them to `build_pod_adapter`).

## OPEN — verify at the live smoke (not resolvable from docs)
- **retail_price submission.** The documented `create-from-template` body does
  not show a per-variant price field; `PodVariantSpec.retail_price` is required
  by our model ("set at product creation"). The smoke must confirm how Gelato
  accepts price (a variant `price`/`retailPrice` field, a separate pricing call,
  or inherited from the template). The adapter will send a best-effort
  per-variant `price` and the smoke verifies/【adjusts】 it. **Flag prominently.**
- Exact `salesChannels`/`productType`/`vendor` values Gelato expects for an
  Etsy store; confirm the draft-visibility behaviour of
  `isVisibleInTheOnlineStore=false` on Etsy specifically.

## Testing (respx — no live calls)
- `create_product`: respx-mock the create endpoint → assert the request body
  (templateId, variants mapping, **`isVisibleInTheOnlineStore=false`**,
  print_file_url in imagePlaceholders, X-API-KEY header) and that the response
  maps to `PodProduct(created, etsy_listing_id=None)`.
- `get_product`: respx a `status:"active"` + `externalId:"5001"` → `linked`,
  `etsy_listing_id=5001`, `etsy_listing_state="draft"`; a `publishing_error` →
  `failed` with the message.
- Error mapping: 4xx/5xx → `PodWriteError` (status + truncated message, no body).
- `delete_product`: respx DELETE 2xx → no raise; non-2xx → `PodWriteError`.
- Factory: `build_pod_adapter(live=True)` (with env stubbed) returns a
  `LiveGelatoAdapter`; live gate open/closed logic.
- **No live Gelato call in any test.**

## Operator smoke (gated — not CI)
With `SHOPSTEWARD_LIVE_GELATO=1` + `GELATO_API_KEY` and real
`store_id`/`template_id`/`variant_key`/`placeholder` in `pod.json`, run
`shop build <a winner> --live-gelato --live-printfile` on ONE winner: confirm a
Gelato product is created, it links to an Etsy **draft** (never live), the
`retail_price` lands correctly (resolve the OPEN item), then `delete_product` to
clean up. Only after this passes is live POD trustworthy.

## Out of scope
- Additional POD providers (Gelato-only).
- Instagram promotion + feedback loop.
