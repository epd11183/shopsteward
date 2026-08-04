# M5b Design — POD Physical Listings (Gelato + Printful)

*Status: DRAFT, pending operator review (PRD §8.2). Architect output 2026-08-03.
Continues `docs/designs/2026-07-14-m5a-digital-listings.md`; M5a §11 named this
milestone and M5a decisions 37–41 are binding here. New decision candidates
42–49 in §16; unresolved questions in §17 — several are load-bearing.*

M5b inverts M5a's flow: **the provider creates the Etsy listing, we enrich it.**
It reuses M5a's data model wholesale (decision 37 promised this: `provider` +
`sku_source` already exist), adds one POD adapter package, and changes Gate 3
from one card per draft to **one card per hero** so a hero that yields a digital
listing *and* two physical listings is still ONE tap.

## 0. Provider-API reality check (2026-08-03) — READ FIRST

The operator asked for both Gelato and Printful. Verified against the vendors'
own documentation today:

**Gelato — POD-first works.** `POST
https://ecommerce.gelatoapis.com/v1/stores/{storeId}/products:create-from-template`
creates the product from a dashboard-defined template and then creates and
publishes variants and mockups to the connected store in the background — an
asynchronous create with a status to poll, exactly the shape §7.1 assumes.

**Printful — POD-first does NOT work. This is a blocker, not a detail.**

- The Products API docs state it "is not intended and will **never** support
  creating and managing products in external platforms such as Shopify,
  WooCommerce and others."
- The Ecommerce Platform Sync API exposes get-list / get / delete sync product,
  get / modify / delete sync variant — **there is no create endpoint.** It
  synchronises products that already exist in the platform.
- `POST /store/products` targets Printful's own "Manual orders / API platform"
  store type, not a connected Etsy store.
- Printful's help centre confirms the only push path is manual: "Add products
  directly from your Printful Dashboard… Products you push to Etsy will first
  appear as Drafts."
- Printful API v2 does not close the gap: "Product management, with sync
  products or product templates, is not available in version 2 of the API yet."

Consequence: a Printful physical listing requires an operator to design and push
the product in the Printful dashboard, per listing. That is a **fourth human
touchpoint in the hero path**, which CLAUDE.md forbids. There is no design that
removes it, because the capability does not exist in the API.

**SUPERSEDED — see §0a. The operator has no Printful account, so Printful is
dropped entirely.** The manual-adopt design below is retained only as the record
of a decision made on a premise that turned out to be wrong.

*Original decision 2026-08-03 (§17 Q0 = option b): Printful stays, as a manual
flow.* Two paths therefore existed, and only one of them was automated:

- **`mode: "api_create"` (Gelato)** — the hero path, fully unattended, §7.1.
- **`mode: "manual_adopt"` (Printful)** — the operator designs and pushes the
  product in the Printful dashboard, which lands an Etsy **draft**; ShopSteward
  then *discovers* that draft, adopts it into a `listingdraft`, and enriches it
  with exactly the same §7.2 code. The eyes-open cost is a fourth touchpoint on
  Printful listings only; the hero/digital/Gelato path keeps its three gates.

The adopt path reuses everything downstream of `pod_linked` — that is why it
costs one slice (§13 slice 6) rather than a second pipeline. `PodAdapter` is not
involved in it at all: there is no product to create, so there is nothing for the
Protocol to do. Printful appears in `pod.json` as catalog + routing entries with
`mode: "manual_adopt"`, and never as a live adapter.

Also resolved from the docs, so no longer operator questions: Printful would be
**v1 sync-products**, not v2 (former Q11); and Printful's Etsy pushes land as
drafts (part of former Q7 — Gelato's draft behaviour is still unverified and
remains a halt condition, §17 Q7).

## 0a. Printful dropped (operator, 2026-08-03, later the same day)

**New fact: the operator has no Printful account.** The manual-adopt compromise
in §0 was chosen on the implied premise that one already existed, so accepting a
fourth touchpoint bought an existing fulfilment relationship. It buys nothing.
Setting up a fresh Printful account would mean deliberately choosing the one
vendor of the three whose API cannot create an Etsy listing.

**M5b is Gelato-only.** Deleted from the design, and from slice 1's shipped code:

- slice 6 (`mode: "manual_adopt"`, the `pod adopt` command, the Etsy-draft
  discovery/adoption path) — **no longer built**
- `PodProviderCatalog.mode` — no second mode exists, so the field goes
- the `printful` entry in `config/defaults/pod.json` `catalog`
- the `canvas` routing rule's `["printful","gelato"]` ordering, **which is the
  live routing defect found in review** (selection could emit a variant for a
  provider with no adapter, and printful being first permanently shadowed
  gelato). Dropping Printful fixes it by deletion rather than by a `mode` filter.
- `PodVariantSpec.placement` and `PodCatalogVariant.placement` — Printful-only
  fields, confirmed dead by review

**PRINTFUL: CLOSED BY ENUMERATION 2026-08-04. Do not research this a third time.**
Printful's own official Postman collection
(`developers.printful.com/docs/postman/printful_postman_collection.json`) lists
**89 endpoints across v1 and v2 and contains ZERO product-creation endpoints.**
Every write is fulfillment: `/orders`, `/v2/orders`, `/order-items`, `/files`,
`/mockup-generator/create-task`, `/shipping/rates`, `/webhooks`,
`/approval-sheets`. Product templates expose **only DELETE**. The
`POST /store/products` the prose docs describe does not appear in the collection
at all, and the docs restrict it to a "Manual orders / API platform" store whose
products "do not appear in external stores like Etsy" regardless.

**Printful's API is an order-fulfilment API, not a catalogue API.** That single
sentence explains every dead end: no product creation, no template creation, no
listing creation, and no push to a connected store.

Consequence for the three canvas options (the operator's only proven physical
product, 77% of revenue from 2 of 7 orders):

- **A — rebuild canvas in Gelato.** Runs on the merged M5b machinery, zero new
  architecture, ~15 min of operator time. **Recommended.** Open question is
  whether Gelato's canvas matches the Printful canvas that actually sold.
- **B — Printful manual + adopt.** Operator designs and pushes in the dashboard
  (2–3 min/listing); ShopSteward adopts the Etsy draft and enriches it. Costs
  the adopt slice that was cut in §0a.
- **C — ShopSteward owns fulfilment.** *Simpler than first assessed:* it needs
  no Printful product at all, because `POST /orders` accepts catalogue variant
  ids and a print-file URL directly. We create the Etsy listing (M5a), then post
  an order per sale. Fully automated, supplier-agnostic, and it removes the
  platform-connection dependency entirely — but it makes us the order router
  (detect sale, map variant, create order, return tracking, catch the
  buyer-paid-nothing-shipped case). A milestone of work and a new risk class to
  protect a supplier choice on two sales. Revisit if canvas volume makes
  supplier choice a margin decision.

**Second provider: open, and Printful is not the candidate.** Printify exposes
`POST /v1/shops/{shop_id}/products.json` plus a publish endpoint with a
documented 200-per-30-minutes publish rate limit — but it *also* exposes
`publishing_succeeded.json` / `publishing_failed.json`, which are callbacks for
an integration where the CALLER is the sales channel. Whether `publish.json`
drives Printify's own native Etsy push, and whether the resulting listing is a
draft or active, could not be settled from public documentation. **Unverified —
do not design on it.** It resolves with a free account and one test listing, or a
support ticket. Until then the routing table stays ordered config, so adding a
verified second provider is an edit, not a redesign.

## 1. Dataflow — and where it diverges from M5a

```
proj_landing_files (valid) --+                                    [M5a, unchanged]
proj_mockups (M4) -----------+--> listings build --> digital draft --> create_draft_listing
proj_scores (M3) ------------+          |                              upload_image[] + upload_file
config/defaults/listing.json -+         |                              update_listing
                                        |
config/defaults/pod.json (DB-seeded) ---+--> pod build (OFFLINE, unattended)  [M5b, NEW]
                                             |
   1 catalog.select_variants()  aspect + DPI + routing -> variants   listingdraft.variants_selected
   2 pod.pricing                cost*markup, solved against floors   listingdraft.priced
   3 printfile.prepare/host     print master -> bytes -> URL         .print_file_prepared/.print_file_hosted
   4 PodAdapter.create_product()   PROVIDER creates the Etsy draft   .pod_create_attempted/.pod_product_created
   5 PodAdapter.get_product()      poll until etsy_listing_id known  .pod_linked
   6 ENRICH (EtsyWriteAdapter)  title/tags/desc + OUR mockups only   .pod_enriched
                                        |
                    +------------- GATE 3 (React) -------------+
                    | ONE card per hero: digital + N physical  |
                    | ONE tap publishes every listing on it    |
                    +---------------------+--------------------+
                                          v  gate3.hero_approved -> publish_listing() per draft
```

**Reused from M5a, unchanged:** `listings/config.py` seed/hash pattern,
`copy.py::generate_copy` + the `CopyAdapter`, `images.py::order_listing_images`
and `sellable_file_bytes` (the deterministic TIFF→sRGB-JPEG re-encode, now with
a POD byte cap), `pricing.py::compute_economics`, `proj_listing_drafts`,
`gate3.py` publish/edit/queue, `listings/api.py` router, `live_gate.py` shape,
and `EtsyWriteAdapter.update_listing` / `upload_listing_image` /
`publish_listing` / `delete_listing`.

**Bypassed on the POD path, deliberately:**

| M5a step | Why POD skips it |
|---|---|
| `create_draft_listing` | The provider creates the listing. Calling this would produce an unlinked duplicate. |
| `upload_listing_file` | Physical listings carry no digital file. |
| `update_listing_price` (updateListingInventory) | **Forbidden.** That endpoint PUTs the whole `products` array — it would rewrite provider-set SKUs and variation structure (CLAUDE.md). POD prices are set *inside the provider's create call*, so the variation structure is authored once, by the provider, with our prices already in it. |
| `push.py::push_drafts` eligibility (`etsy_listing_id IS NULL`) | POD receives its listing id from the provider, not from us. |
| M5a's per-draft Gate 3 card | Replaced by a per-hero card (§8). |

## 2. Module / file map

```
src/shopsteward/adapters/pod/interface.py    PodAdapter Protocol + PodWriteError (EtsyWriteError twin).
src/shopsteward/adapters/pod/models.py       PodProviderRef, PodVariantSpec, PodProductSpec, PodProduct.
src/shopsteward/adapters/pod/fake.py         FakeGelatoAdapter (in-memory twin, default everywhere;
                                             enforces the draft invariant).
src/shopsteward/adapters/pod/gelato.py       LiveGelatoAdapter   (httpx only, X-API-KEY).
                                             (no printful.py — mode "manual_adopt" needs no adapter, §0)
src/shopsteward/adapters/printfile/interface.py  PrintFileHost Protocol + HostedFile.
src/shopsteward/adapters/printfile/fake.py       FakePrintFileHost (in-memory, https://fake.invalid/<key>).
src/shopsteward/pipeline/listings/pod/config.py    pod.json load/hash/seed/get (listings/config.py twin).
src/shopsteward/pipeline/listings/pod/models.py    PodConfig, PodVariant, PodBuildReport.
src/shopsteward/pipeline/listings/pod/catalog.py   PURE: aspect_of(w,h), effective_dpi(), route(),
                                                   select_variants() -> (kept, dropped[{format,reason}]).
src/shopsteward/pipeline/listings/pod/pricing.py   PURE: retail_price(unit_cost, rules) closed-form
                                                   floor solve; pod_economics().
src/shopsteward/pipeline/listings/pod/printfile.py print-file resolution (reuses images.sellable_file_bytes)
                                                   + host publish/revoke.
src/shopsteward/pipeline/listings/pod/build.py     Orchestrator: select -> price -> printfile -> create ->
                                                   poll/link -> enrich. Idempotent by draft_id.
src/shopsteward/pipeline/listings/pod/enrich.py    Etsy enrichment: copy reuse, image trim+upload,
                                                   update_listing (title/desc/tags ONLY).
src/shopsteward/pipeline/listings/pod/factory.py   build_pod_adapter(provider, live), build_print_file_host(live).
config/defaults/pod.json                      schema shopsteward.pod/1 (§6). SEPARATE from listing.json.
src/shopsteward/adapters/etsy/{interface,live,fake}.py  + list_listing_images / delete_listing_image.
src/shopsteward/pipeline/listings/{projections,gate3,api,cli,models}.py  extended in place.
frontend/src/pages/Gate3.tsx                  hero card (N listings, one Publish).
tests/adapters/pod/, tests/adapters/printfile/, tests/pipeline/listings/pod/
```

Living under `pipeline/listings/pod/` means **no new import-linter contract** —
listings is already inside `pipeline`. Two contracts gain module names:
`shopsteward.adapters.pod` and `shopsteward.adapters.printfile` are added to the
editing-standalone `forbidden_modules` and to the "pipeline imported by no lower
layer" `source_modules` (the stale `adapters.printful` / `adapters.gelato`
entries stay; harmless). That is a `pyproject.toml` amendment → §8.2 review.

**pod.json is a separate config file, not a section of listing.json.** Folding it
in would change `config_hash()`, which would change every existing digital
`draft_id` and orphan every built draft. POD carries its own `pod_config_hash`.

## 3. Events, projections, idempotency

Reuses the `listingdraft.*` namespace and `proj_listing_drafts` (decision 37).
Dot-separated, past tense, immutable, `user_id` on every row.

| Event | Payload |
|---|---|
| `podconfig.seeded` / `.updated` | `{name:"default", config:{...}, source:"defaults"\|"operator"}` |
| `listingdraft.created` | **reused**: `{draft_id, landing_file_id, photo_id, set_key, provider:"gelato", format:<product_type>, sku_source:"provider", listing_type:"physical", config_hash, pod_config_hash}` |
| `listingdraft.variants_selected` | `{draft_id, aspect, source_px:[w,h], variants:[{format,size,aspect,variant_key,dpi}], dropped:[{product_type,reason:"aspect"\|"dpi"\|"above_max_price"\|"no_route"\|"no_variant"}]}` — `variants` is every surviving size for the product type, not one; `dropped` keys on `product_type` so the two namespaces are never confused |
| `listingdraft.priced` | **reused, extended**: `{draft_id, currency, unit_cost, variants:[{format,base_cost,shipping_est,retail_price,net,margin_pct}], costs_verified_on, cost_stale:bool, auto:true}` |
| `listingdraft.copy_generated` | **reused**: + `{copy_source:"hero_shared"\|"generated", title_suffix}` |
| `listingdraft.images_selected` | **reused**: `sellable_file:null` for POD (no digital file) |
| `listingdraft.print_file_prepared` | `{draft_id, source:"landing_original"\|"derived_jpeg", sha256, bytes, long_edge_px}` |
| `listingdraft.print_file_hosted` | `{draft_id, host:"<name>", file_key, expires_at, sha256}` — **never the URL.** A signed URL in an append-only log is an undeletable credential (decision 35's token rule). |
| `listingdraft.pod_create_attempted` | `{draft_id, provider, variant_count, print_file_sha256}` — written *before* the call so a crash leaves a marker |
| `listingdraft.pod_product_created` | `{draft_id, provider, provider_product_id, status, variant_count}` |
| `listingdraft.pod_linked` | `{draft_id, provider_product_id, etsy_listing_id, state:"draft", polls}` |
| `listingdraft.pod_link_timed_out` | `{draft_id, provider_product_id, waited_seconds, last_status}` |
| `listingdraft.provider_images_trimmed` | `{draft_id, etsy_listing_id, deleted:[{etsy_image_id,rank}]}` |
| `listingdraft.pod_enriched` | `{draft_id, etsy_listing_id, fields:["title","description","tags"], images:[{etsy_image_id,rank,intent}]}` |
| `listingdraft.pod_failed` | `{draft_id, provider_product_id?, etsy_listing_id?, stage:"select"\|"price"\|"printfile"\|"host"\|"create"\|"link"\|"enrich_images"\|"enrich_update", error:{code,message}}` |
| `listingdraft.pod_skipped` | `{draft_id?, landing_file_id, reason}` — no variant survived selection/pricing; a hero with no viable physical SKU is normal, not a failure |
| `gate3.hero_approved` | `{hero_key, draft_ids:[...], total_price, currency}` — the one tap |
| `gate3.approved` / `.published` / `.publish_failed` | **reused, per draft**, emitted once per listing under the one tap |

`proj_listing_drafts` gains columns (drop-and-rebuild — **no migration**):
`pod_config_hash, provider_product_id, pod_status, variants_json, unit_cost,
print_file_sha256, print_file_key`. POD state machine folds into the existing
`state` column: `built → pod_created → pushed` (on `pod_enriched`) `→ published`,
plus `push_failed` (from `pod_failed`), `publish_failed`, and one new value
`pod_unverified`. Gate 3's existing `state IN ('pushed','push_failed',
'publish_failed')` queue therefore picks POD drafts up unchanged.

**Idempotency.** `draft_id = sha256(landing_file_id | pod_config_hash | provider
| product_type)` — a distinct id space from digital drafts. Skip predicate:

- `provider_product_id IS NOT NULL` → never call `create_product` again, ever. A
  second create means a duplicate provider product *and* a duplicate Etsy listing.
- `pod_create_attempted` present, `pod_product_created` absent → state
  `pod_unverified`. **Not auto-retried.** It surfaces in the Gate 3 failed drawer
  as "verify in the provider dashboard, then Retry or Discard". Gelato offers no
  client-supplied idempotency key we can rely on, so an automatic retry here is
  how you get two products. This is an exception path, not the hero path — it
  adds no touchpoint to a successful run.
- Linked-but-unenriched, or partially enriched → fill-forward, per stage, exactly
  like M5a's reconciled skip predicate. Already-attached image ranks are skipped
  via the existing `_images_stage(skip_ranks=…)` mechanism.
- `state='published'` → never rebuilt, `--force` or not. `--force` re-runs
  selection/pricing/copy only; it never re-creates a provider product.

## 4. Adapter interfaces

### 4.1 `adapters/pod/interface.py`

```python
class PodVariantSpec(BaseModel):
    format: str                    # our format key, e.g. "framed_poster_16x20"
    variant_key: str               # OPAQUE. gelato: templateVariantId | printful: catalog variant id
    placeholder: str | None = None # gelato imagePlaceholders[].name  ("ImageFront")
    placement: str | None = None   # printful files[].type            ("default")  (deferred)
    fit_method: str | None = None  # gelato fitMethod                 ("slice")
    retail_price: float            # set HERE, at product creation -- never via Etsy inventory

class PodProviderRef(BaseModel):
    provider: str                  # "gelato"  (+ "printful" if §17 Q0 revives it)
    store_id: str
    template_id: str | None = None # gelato only; None => non-template create
    variants: list[PodVariantSpec]

class PodProductSpec(BaseModel):
    ref: PodProviderRef
    title: str
    description: str
    tags: list[str]
    print_file_url: str            # transient. NEVER persisted to an event or logged.
    publish_as_draft: bool = True  # write-safety invariant, decision 41's POD twin

class PodProduct(BaseModel):
    provider_product_id: str
    status: str                    # NORMALISED: created|publishing|linked|failed
    etsy_listing_id: int | None
    etsy_listing_state: str | None # must be "draft" when linked
    variant_count: int
    error: str | None = None

class PodAdapter(Protocol):
    def create_product(self, spec: PodProductSpec) -> PodProduct: ...
    def get_product(self, provider_product_id: str) -> PodProduct: ...
    def delete_product(self, provider_product_id: str) -> None: ...   # smoke cleanup ONLY
```

**Why a Protocol with one implementation.** CLAUDE.md requires every external
system to sit behind an adapter interface; the Protocol *is* that boundary, and
the fake that satisfies it is the second implementation in practice — every test
runs on it. The shape below is also what a second provider would need, so the
table stays, marked for what it costs.

| Concern | Gelato | Printful *(deferred)* | Unifies? |
|---|---|---|---|
| Auth | `X-API-KEY: <key>` | `Authorization: Bearer` + `X-PF-Store-Id` | Yes — inside each impl |
| Create in a connected Etsy store | `POST ecommerce.gelatoapis.com/v1/stores/{storeId}/products:create-from-template` | **Not possible via API** (§0) | — |
| Variant identity | opaque `templateVariantId` + `productUid` descriptor | numeric catalog `variant_id` | **Leaks.** Core never parses it: `variant_key: str`, supplied by config. |
| Variant *structure* | defined server-side, in a Gelato dashboard template | enumerated in the request | **Leaks.** `template_id` populated for Gelato, `None` otherwise. The one genuinely non-unifying field. |
| Print file | `imagePlaceholders[].fileUrl` | `files[].url` | Both **URL-only, no binary upload** → §4.2 |
| Etsy linkage | asynchronous; variants and mockups publish in the background; `externalId` appears late | n/a | Needs `get_product` polling; statuses normalise to four values |
| Draft vs active on Etsy | **unverified — §17 Q7, a halt condition** | drafts (help-centre confirmed) | — |
| Base cost | `GET product.gelatoapis.com/v3/products/{productUid}/prices` | catalog/estimate endpoints | Not in the interface — cost comes from config (§5) |
| Provider mockups | preview URLs returned free | async mockup-generator task | Not in the interface — M4 already produces better ones (§7.3) |
| Orders/fulfilment | order webhooks | order webhooks | **Not in the interface.** Out of scope (§11) |

### 4.2 `adapters/printfile/interface.py` — the constraint nobody wants

Gelato fetches the print file from a URL we supply; it does not accept uploaded
bytes. A local-first tool has no such URL. That makes a print-file host a **new
external service** and therefore an operator decision (§17 Q1), not an
architect's pick.

```python
class HostedFile(BaseModel):
    key: str
    url: str            # transient; returned to the caller, never evented
    expires_at: str

class PrintFileHost(Protocol):
    def publish(self, data: bytes, *, name: str, ttl_seconds: int) -> HostedFile: ...
    def revoke(self, key: str) -> None: ...
```

`FakePrintFileHost` returns `https://fake.invalid/<sha256>` and satisfies slices
1–4 entirely offline. **The host decision blocks only the live smoke test**, so
implementation is not gated on it.

### 4.3 `EtsyWriteAdapter` additions

```python
def list_listing_images(self, listing_id: int) -> list[EtsyImageRef]: ...
def delete_listing_image(self, listing_id: int, listing_image_id: int) -> None: ...
```

Needed to make room for our M4 mockups (§7.3). Images are not SKUs or
variations, so trimming them does not violate the POD-first rule — but it *is*
deleting provider content, so it is config-gated and operator-reviewable (§17 Q9).
The Etsy scope required for `deleteListingImage` (`listings_w` vs `listings_d`)
is unverified — §17 Q10.

## 5. Routing, catalog/variant mapping, pricing

**Routing (§17 Q3/Q4 supply the values).** `pod.routing` is an ordered list of
rules keyed `(product_type, region)` → ordered `providers[]`. First match wins;
evaluation is a pure function of config. With one provider the list has one
entry per product type, all naming `gelato` (Printful dropped — §0a).
**Fallback:** walk the provider list in order and take the first one whose
`catalog[provider].products[product_type]` contains at least one variant matching
the photo's aspect and clearing the DPI floor. If no rule matched at all, drop
with `reason:"no_route"`; if rules matched but no routed provider stocks a
same-aspect variant, drop with `reason:"no_variant"` — the two imply different
operator repairs (edit routing vs. add a SKU), which is the whole point of
recording them. Neither is ever escalated to the operator at a gate. Region has
exactly one value in v1 (§17 Q8).

**Drop-reason precedence is an explicit ordering, not assignment order:**
`dpi > above_max_price > no_route > no_variant`. DPI wins because it is the only
reason the operator *cannot* fix in config — it is a property of the photograph.
Slice 2 adds `above_max_price` by adding a table entry, not a nested branch.

**Limitation, accepted: price does not participate in provider fallback.**
Pricing runs after selection, so a product type whose routed provider prices
above `max_price` is dropped rather than re-routed to a cheaper second provider.
With one automated provider this is unreachable; write it down before it is
rediscovered as a bug.

**Catalog/variant mapping — deterministic, zero human input.**

1. `aspect_of(width, height)` from `proj_landing_files` → nearest of the aspect
   classes declared in config, within `print_file.aspect_tolerance` (default
   0.02). No match → `pod_skipped`.
   **ORIENTATION IS PART OF THE MATCH (confirmed defect, 2026-08-04).** The
   shipped slice-1 code computes `ratio = max(w,h)/min(w,h)`, discarding
   orientation, and `PodCatalogVariant` has no orientation field — so a portrait
   and a landscape photo of the same ratio select the identical SKU, and with
   `fit_method:"slice"` the provider centre-crops the mismatch. The operator's
   Gelato templates are **landscape-only**, so every portrait hero would route to
   a landscape product and be side-cropped. The repair is not expressible in
   config: `ratio` is always ≥ 1, so a `"5:4": 0.8` entry can never match, and a
   `1.25` entry ties and loses the strict `diff < best_diff` tie-break. Fix:
   `aspect_of` returns `(class, orientation)`, `PodCatalogVariant` declares its
   own orientation, and selection matches on both. A variant whose template
   genuinely accepts either orientation declares `orientation: "any"`.
2. `pod.formats_by_aspect[aspect]` → candidate product types. Fixed list, config.
3. Routing (above) picks the provider per product type.
4. **Every** same-aspect variant of that product type is a candidate, and each is
   dropped individually if `effective_dpi = long_edge_px / long_edge_inches <
   print_file.min_dpi` (default 150) or if its priced retail exceeds
   `pricing.max_price`. A product type has N sizes; taking only the first would
   silently sell one size.
5. Survivors become `PodVariantSpec[]` — one provider product per product type,
   carrying every size that cleared.

Every branch reads config; nothing asks the operator; the outcome is a pure
function of (pixel dimensions, pod.json). That is the answer to "Gate 3 stays
one tap": **variant and size selection is a config decision made once, not a
per-hero decision made at the gate.**

**Pricing — closed form, composes with M5a.** The Etsy fee model is *not*
duplicated: POD reads `pricing.etsy_fees` from `listing.json`. Let
`f(p) = listing_fee + p·(transaction_pct + payment_pct) + payment_flat` and
`unit_cost = base_cost + (shipping_est if shipping_included else 0)`.

```
p_markup = unit_cost * markup
p_abs    = (margin_floor_abs + unit_cost + listing_fee + payment_flat) / (1 - t - pp)
p_pct    = (unit_cost + listing_fee + payment_flat) / (1 - t - pp - margin_floor_pct)
retail   = round_up_to_ending(max(p_markup, p_abs, p_pct))
```

Both floors are satisfied by construction, so `enforce_floor` can never fail
silently on the auto path; it is still asserted afterwards as a guard.
`compute_economics(price, rules, unit_cost=0.0)` gains one optional argument —
`net = price - fees - unit_cost`, plus `margin_pct` — and M5a's digital call
sites are unchanged (`unit_cost` defaults to 0). Gate 3 shows per-variant
`{price, etsy_fees, unit_cost, net, margin_pct}` and a hero roll-up of the
worst-margin variant.

**PRICING DECISION 2026-08-04 (operator delegated pricing; slice 2 implements).**
Cost-plus markup is the automatic **guard**, not the price setter. Add
`retail_override: float | None` to `PodCatalogVariant`; when present it is the
retail price, and `enforce_floor` still asserts it clears both margin floors
(an override below the floor is a config error, not a silent discount). When
absent, the closed-form markup/floor solve above applies unchanged.

Shipped values and why:

| Variant | Cost | Price | Rationale |
|---|---|---|---|
| acrylic_16x24 | 43.53 | **149** | operator's existing live price |
| acrylic_20x30 | 62.28 | **179** | ditto |
| acrylic_24x36 | 71.09 | **229** | ditto |
| poster_12x18 | 14.15 | **39** | value ladder, not cost-plus |
| poster_16x24 | 15.59 | **59** | ditto |

*Acrylics:* ~100 live listings already carry these prices. Computed markup 3.0
would set new listings at 131/187/213, so two customers would see different
prices for the same product depending which listing they found. Shop-wide price
consistency beats a marginally better price chosen without conversion data.

*Posters — the reason the model needed the escape hatch:* Gelato's poster cost
rises only **$1.44** (14.15 → 15.59) for **78% more print area**. Any markup
produces near-identical prices ($42 vs $47), so the larger print always dominates
and the small one never sells. Cost-plus assumes cost tracks perceived value;
for this supplier's paper products it does not. Margins at the chosen prices:
53% and 63% after Etsy fees.

Revisit when M8a's brief supplies real conversion data — these are judgements,
not measurements.

**Stale costs eat margin silently.** `pod.json` carries `costs_verified_on` and
`cost_staleness_days` (90); when the config date is older, `listingdraft.priced`
records `cost_stale:true` and the Gate 3 card shows a warning chip. Three lines
of logic on the money path; no provider price API is called. *Skipped: live
price refresh via the provider catalog endpoints. Add when the catalog exceeds a
dozen SKUs or the first stale-cost warning actually fires.*

## 6. `config/defaults/pod.json` (schema `shopsteward.pod/1`)

```json
{ "schema": "shopsteward.pod/1", "name": "default", "enabled": true,
  "region": "US", "currency": "USD",
  "print_file": { "prefer": "tiff_master", "max_bytes": 100000000,
                  "min_dpi": 150, "aspect_tolerance": 0.02, "host_ttl_seconds": 86400 },
  "aspects": { "4:5": 1.25, "2:3": 1.5, "1:1": 1.0 },
  "formats_by_aspect": { "4:5": ["framed_poster","poster"], "2:3": ["framed_poster","poster","canvas"],
                         "1:1": ["poster","canvas"] },
  "routing": [ {"product_type":"framed_poster","region":"US","providers":["gelato"]},
               {"product_type":"poster","region":"US","providers":["gelato"]},
               {"product_type":"canvas","region":"US","providers":["gelato"]} ],
  "catalog": {
    "gelato":   { "store_id_env":"GELATO_STORE_ID",
                  "products": { "framed_poster": { "template_id":"<OPERATOR>",
                    "variants":[ {"format":"framed_poster_16x20","size":"16x20in","aspect":"4:5",
                                  "orientation":"landscape",
                                  "long_edge_inches":20,"variant_key":"<OPERATOR>",
                                  "placeholder":"ImageFront","fit_method":"slice",
                                  "base_cost":0.00,"shipping_est":0.00} ] } } } },
  "costs_verified_on": "1970-01-01", "cost_staleness_days": 90,
  "pricing": { "markup": 2.6, "price_ending": 0.00, "margin_floor_abs": 8.00,
               "margin_floor_pct": 0.25, "max_price": 250.00, "shipping_included": true },
  "copy": { "title_suffix": { "framed_poster": " — Framed Fine Art Print",
                              "poster": " — Fine Art Print", "canvas": " — Gallery Canvas" },
            "description_block": { "framed_poster": "Printed and framed on demand…" } },
  "images": { "max_ours": 5, "hard_cap": 10, "trim_provider_images": true },
  "link_timeout_seconds": 180, "link_poll_interval_seconds": 10 }
```

Shipped `variant_key`s, `template_id`s and `base_cost`s are `<OPERATOR>` /
`0.00` placeholders — real values are operator-supplied (§17 Q3–Q5), exactly as
M4 shipped synthetic templates (decision 28). `pod_config_hash()` = sha256 of
canonical compact JSON, M4/M5a precedent. `enabled:false` disables the entire
POD phase in one config edit — that is the rollback lever (§15).

## 7. POD mechanics

### 7.1 Creation

`create_product(spec)` is called **once**, with `publish_as_draft=True` and every
variant's retail price already computed. The live adapter raises `PodWriteError`
if the provider returns a product it will publish live rather than as a draft —
the POD twin of `create_draft_listing`'s `state != "draft"` guard. Then poll
`get_product` every `link_poll_interval_seconds` up to `link_timeout_seconds`;
on `linked` emit `pod_linked` with `etsy_listing_id`, on timeout emit
`pod_link_timed_out` and leave the draft resumable (a later `pod build` re-polls
without re-creating). Gelato creates the product immediately and then creates and
publishes variants and mockups in the background, so polling is mandatory, not
defensive. Poll events are *not* written per attempt — only the terminal
transition — so the log does not fill with noise.

### 7.2 Enrichment ordering

Runs only after `pod_linked`. Every step is guarded by its own event, so a
partial failure resumes exactly where it stopped.

1. **Copy.** If a digital draft exists for the same `landing_file_id` and already
   carries `listingdraft.copy_generated`, **reuse its title/tags/description**
   (`copy_source:"hero_shared"`); otherwise make one call. Per-listing title =
   base truncated to `140 - len(suffix)` + `copy.title_suffix[product_type]`.
   Description = base + `copy.description_block[product_type]` + the mockups.json
   AI-disclosure line. **One copy call per hero, not per listing** — N listings
   would otherwise multiply the $10/mo cap by the variant count.
2. **Images.** `list_listing_images` → count provider images. If
   `existing + max_ours > hard_cap` and `trim_provider_images`, delete from the
   highest rank downward until there is room (`provider_images_trimmed`). Then
   upload our M4 mockups at ranks 1..k, ordered by the existing
   `listing.json image_order` (hero `single` at rank 1 = Etsy primary).
3. **Update.** `update_listing(title, description, tags)` — **that is the whole
   field set.** Never `who_made`, `when_made`, `is_supply`, `taxonomy_id`,
   `state`, quantity, inventory, or price. Those are provider-set and, for
   `who_made`/production-partner, are also an Etsy POD *policy* surface.
4. `listingdraft.pod_enriched`.

Re-run on an already-enriched draft: no-op. Re-run mid-way: only the missing
steps. A draft the provider created but we never linked: re-poll only.

### 7.3 Images — which go on the listing

Ours (M4 compositor output) occupy ranks 1..`max_ours`. Provider mockups keep the
remaining slots. We upload ours; the provider already uploaded theirs. We build
**no** provider mockups: Gelato's previews are generic, while M4 already
composites room scenes tuned to this photograph. The print file itself is never
uploaded to Etsy.

### 7.4 The print file

`print_file.prefer` selects the landing artifact (§17 Q13 — the Gate 2 export
preset produces both an AdobeRGB TIFF master and an sRGB JPEG). Resolution reuses
`images.sellable_file_bytes(path, pod.print_file.max_bytes)` verbatim — the same
deterministic max-quality sRGB re-encode M5a already ships, with a POD-sized cap.
No resize, no AI, no generative anything: the photograph is untouched (decision 34).
Hosted with a TTL, `revoke`d after `pod_linked`, and the URL never reaches the
event log or a log line.

## 8. Gate 3 — one card per hero

M5a's queue returns one card per draft. With POD, a hero produces 1 digital + N
physical listings, which under M5a's model would be N+1 taps — a guardrail
violation. Gate 3 therefore groups by hero.

```
GET  /api/pipeline/gate3/queue   -> Gate3HeroCard[]
     { hero_key (= landing_file_id), photo_id, images[],
       listings: [ Gate3Card ],            # M5a's card, unchanged, one per draft
       totals: { listings, worst_margin_pct, cost_stale } }
POST /api/pipeline/gate3/publish { hero_key }   # THE tap: gate3.hero_approved,
                                                # then publish each publishable draft
POST /api/pipeline/gate3/publish { draft_id }   # retained for the failed drawer
POST /api/pipeline/gate3/edit    { draft_id, ... }
```

Partial publish: publish every draft, collect per-draft results, return the hero
card with the failures marked. A retry publishes only what is still unpublished.
`Gate3Card` gains `provider`, `sku_source`, `variants[]`, `unit_cost`,
`provider_product_id`, `cost_stale`.

**Price edits are refused for POD drafts in v1** — `sku_source == "provider"` →
HTTP 400 with "POD retail price is set by the provider at product creation;
change pod.json markup and rebuild". Editing it via Etsy would mean writing
inventory, which is the one thing we may not do. Title/tags/description stay
editable. Upgrade path: a `PodAdapter.update_product_prices()` — not built in
v1 (§17 Q12).

Gate3.tsx: one card per hero, mockup carousel, a compact per-listing strip
(format, price, net, margin), a stale-cost chip, one Publish button.

## 9. Live-gating and secrets (all default OFF)

| Path | Offline default | Live requires |
|---|---|---|
| POD product create | `FakeGelatoAdapter` | `--live-pod` + `SHOPSTEWARD_LIVE_POD_WRITE=1` + provider credential + store id |
| Print-file host | `FakePrintFileHost` | `--live-printfile` + `SHOPSTEWARD_LIVE_PRINTFILE=1` + host credential |
| Etsy enrichment | `FakeEtsyWriteAdapter` | M5a's existing triple gate (`--live-etsy-write` + env + `listings_w` tokens) |
| Copy | `FixtureCopyAdapter` | M5a's existing copy gate |
| Publish | Gate 3 endpoint only | as above |

`live_gate.py` grows `live_pod_open(provider)` / `live_pod_error(provider)`
(provider-keyed, mirroring `live_vision_open`) and `live_printfile_open()`.

**Every credential below is an operator decision (§8.2), none is assumed:**

| Secret | Provider | Home |
|---|---|---|
| `GELATO_API_KEY` | Gelato, `X-API-KEY` on every request | `.env` only |
| `GELATO_STORE_ID` | the connected Etsy store UUID | `.env` only |
| print-file host creds | TBD (§17 Q1) | `.env` only |

No new Etsy *listing* scope beyond M5a's `listings_w` — unless
`deleteListingImage` turns out to need `listings_d`, which would mean another
consent round (§17 Q10). Tokens, keys and signed URLs never enter the event log.

## 10. Test plan (all offline, zero network)

- **`FakeGelatoAdapter`**: in-memory; `create_product` returns
  `status="publishing"`, `get_product` flips to `linked` with a monotonic
  `etsy_listing_id` and `etsy_listing_state="draft"` after n polls; raises if
  `publish_as_draft=False`; raises if the same `(print_file_sha256,
  variant_keys)` is created twice (duplicate detector); `calls` list for
  assertions; enforces Gelato's own requirements (`template_id` and
  `placeholder` present).
- **`FakePrintFileHost`**: `publish` → deterministic `https://fake.invalid/<sha>`;
  `revoke` removes; asserts the URL is never seen in any appended event payload.
- **Live-adapter tests**: `respx` against request/response JSON shaped from
  Gelato's public docs now, replaced by recorded + scrubbed real fixtures after
  the §14 smoke. Never commit live ids, store ids, or template ids.
- **Pure-function tests**: `catalog.select_variants` (aspect match, DPI drop,
  routing fallback, no-provider skip), `pricing.retail_price` (both floors
  binding, `max_price` drop, price ending), reusing `tests/pipeline/listings/`
  helper conventions.
- **E2E `tests/pipeline/listings/pod/test_e2e_pod_gate3.py`** — seed a 4000×5000
  landing file + M4 mockup set + M3 verdict, build the digital draft, then
  `pod build` with all fakes, and assert:
  - variants selected deterministically; every retail price ≥ both floors;
  - `create_product` called exactly once per product type; `pod_linked` recorded;
  - enrichment attached our `single` mockup at rank 1 and called `update_listing`
    with **only** title/description/tags;
  - **`("create_draft_listing", …)` appears only for the digital draft, and
    `("update_listing_price", …)` never appears at all** — the two guardrails;
  - the copy adapter was invoked once for the hero, not once per listing;
  - Gate 3 queue = ONE hero card carrying its listings; ONE publish call →
    `gate3.hero_approved` + one `gate3.published` per listing;
  - re-run → `skipped_idempotent`, zero new provider calls;
  - zero `llm.call` events, zero network.

## 11. Non-goals (explicit)

A live Printful adapter (`mode: "manual_adopt"` needs none — §0); order/fulfilment
sync, provider webhooks, tracking numbers,
shipping profiles (the provider owns them); provider mockup generation; POD order
placement (Etsy → provider fulfilment is the provider's connected-store job);
variation or SKU editing of any kind; post-publish price sync back to the
provider; inventory sync; provider catalog auto-discovery (catalog is hand-entered
config); multi-region and multi-currency; bundle proposer (still too small a
catalog); Instagram (M6); Stage 5 listing management; a second file host;
retiring/relisting POD products.

## 12. Rejected alternatives

- **Dropping Printful entirely.** Offered as §17 Q0 option (a); the operator
  chose (b) instead — accepting a fourth touchpoint scoped to Printful physical
  listings in exchange for a second fulfilment source. Recorded as a deliberate,
  bounded exception rather than an erosion: the discovery job never runs on the
  hero path, and a Printful listing that is never pushed simply never appears.
- **A second automated pipeline for the adopt path.** Everything downstream of
  `pod_linked` is identical. Adoption is a discovery query plus a synthetic
  `pod_linked`, not a parallel build.
- **One adapter that also does orders/fulfilment/mockups.** Three subsystems we
  are explicitly not building; a Protocol with unimplemented methods is worse
  than no Protocol.
- **We create the Etsy physical listing, then link the provider to it.** Violates
  POD-first (CLAUDE.md, PRD §5.4) and neither provider's linkage model supports
  adopting a foreign listing without dashboard work.
- **Set POD prices via `update_listing_price` / updateListingInventory.** That PUT
  rewrites the whole `products` array — provider SKUs and variation structure
  included. Prices go in the provider's create call instead.
- **Base64 / data-URL / multipart print files.** Gelato fetches a URL and does not
  accept bytes. There is no design that avoids a file host.
- **Store the signed print-file URL in the event log.** Append-only means an
  undeletable credential (decision 35's rule). Only `host` + `file_key` are evented.
- **One Gate 3 card per POD listing.** Three listings per hero = three taps = the
  fourth-touchpoint failure CLAUDE.md forbids.
- **Ask the operator to pick sizes/variants at Gate 3.** Same failure. Sizes are a
  config decision made once (§5).
- **A `pod` config section inside `listing.json`.** Changes `config_hash()` and
  orphans every existing digital draft_id.
- **A top-level `src/shopsteward/pod/` package.** Would need a new import-linter
  contract and would duplicate `listings`' config/projection/Gate-3 machinery;
  `pipeline/listings/pod/` reuses all of it and changes no contract structure.
- **One copy call per listing.** N× the LLM spend for text that differs by a
  config-supplied suffix and a boilerplate paragraph.
- **Auto-retrying `pod_create_attempted` without a confirmed product.** Produces
  duplicate provider products and duplicate Etsy listings. Manual verify instead.

## 13. Implementation slices (dependency order)

| # | Scope | Size | Mergeable with tests |
|---|---|---|---|
| **1** ⭐ | `adapters/pod/{interface,models,fake}.py` + `adapters/printfile/{interface,fake}.py` + `config/defaults/pod.json` + `pod/config.py` (seed/hash/get) + `proj_pod_config` + `pod/catalog.py` (pure selection/routing) + import-linter amendment. No network, no new dependency. | 1 evening | fake contract tests + pure selection tests |
| **2** | `pod/pricing.py` (closed-form floors), `compute_economics(unit_cost=…)` extension, `pod/printfile.py`, `pod/build.py` through `listingdraft.created/.variants_selected/.priced/.print_file_prepared/.print_file_hosted`, projection columns, `shopsteward pod build --dry-run`. **Carry-forward from slice 1:** a per-variant drop *inside a winning product type* is currently discarded (selection returns on success and the reasons set goes with it), so an operator never learns that 30x40 failed DPI while 16x20 shipped. `above_max_price` drops individually too, so give `PodDroppedVariant` an optional `format` and record variant-granular drops. | 1 evening | pricing edge cases + build-stage E2E to "priced" |
| **3** | `LiveGelatoAdapter` (httpx, respx-tested), `pod/factory.py`, `live_gate` additions, create + poll + link stages, `pod_failed` / `pod_unverified` handling, CLI/API live flags. | 1 weekend | respx live-shape tests + fake-backed link/timeout/resume tests |
| **4** | `pod/enrich.py` + `EtsyWriteAdapter.list_listing_images` / `delete_listing_image` (live + fake) + image trim/upload + `update_listing` (3 fields) + resume. | 1 evening–weekend | the two guardrail assertions (§10) + partial-failure resume |
| **5** | Gate 3 hero grouping: `Gate3HeroCard`, `/gate3/publish {hero_key}`, `gate3.hero_approved`, POD price-edit refusal, `Gate3.tsx`. | 1 evening | full E2E: one tap → N published |
| ~~6~~ | ~~`mode: "manual_adopt"` (Printful)~~ — **CUT 2026-08-03 (§0a).** The operator has no Printful account; the adopt path served no real provider. If a verified second provider lands later it goes through `PodAdapter` like Gelato, not through an adopt path. | — | — |

**Slice 1 is the first PR of the milestone** → operator review is mandatory
regardless (PRD §8.2: first PR of a milestone, adapter-interface change,
`pyproject.toml` amendment, and a new external service all land in it).

## 14. Gated live-write smoke test (mirrors M5a §8.4)

**Must be true before the operator approves it:**

1. Slices 1–5 merged; fakes and the full offline suite green; zero network in tests.
2. Fixtures recorded from Gelato's public docs and reviewed; scrubbing script ready.
3. Etsy shop connected inside the Gelato dashboard, set to publish new products
   **as drafts**.
4. Etsy production-partner declaration in place for the shop (Etsy POD policy).
5. Print-file host decided (§17 Q1), live, with non-guessable expiring URLs.
6. `pod.json` populated with real `template_id`s, `variant_key`s and real base
   costs; `costs_verified_on` set to today.
7. The print master used is a **throwaway test image**, never a sellable photograph.
8. Rollback written down and rehearsed (below).

**Run:** one product, one variant — `shopsteward pod build --photo-id <test>
--provider gelato --live-pod --live-printfile --live-etsy-write`.

**Verify:** (a) the Etsy listing exists and is `state=draft` — *if it is `active`,
HALT the milestone's live path and re-decide*; (b) `GET /listings/{id}/inventory`
captured **before and after** enrichment is byte-identical in SKUs, property
values and offerings; (c) our M4 mockup is image rank 1; (d) title/tags/description
are ours; (e) retail price matches the computed value; (f) `who_made`,
`when_made`, `taxonomy_id` unchanged.

**Cleanup:** `delete_product` on the provider → confirm the Etsy listing is gone
→ if it survives, `EtsyWriteAdapter.delete_listing` → `revoke` the hosted print
file → scrub and commit fixtures. Never commit live ids.

## 15. Guardrail impact · smallest proving test · rollback

**Guardrail impact.** POD-first: honoured — the provider creates, we enrich, and
the one Etsy endpoint that could rewrite variations is architecturally excluded
(price moves into the provider call). AI-never-touches-the-photograph: unchanged;
the print file is a deterministic re-encode. Three gates: preserved by making
Gate 3 hero-grouped rather than draft-grouped; variant/size selection is config,
not a gate — and Printful is excluded precisely because it would add a gate.
Event-sourcing: append-only, no UPDATE/DELETE, new types named in §3, projections
drop-and-rebuild so **no migration**. Config-over-code: catalog, routing, pricing,
image policy and copy suffixes all in `pod.json`. Adapters: core imports no
provider SDK; httpx only; **no new Python dependency**. Editing boundary:
untouched, plus two module names added to the contracts. `user_id`:
`proj_pod_config` and the extended `proj_listing_drafts` both carry it. Net new
risk: one new external service class (a print-file host) — flagged, not assumed.

**Smallest test that proves it works.** `test_e2e_pod_gate3.py`: a hero with one
digital + two physical drafts, built entirely on fakes, asserting
`("update_listing_price", …) not in etsy.calls`, `create_draft_listing` count == 1,
and one `/gate3/publish {hero_key}` producing three `gate3.published` events.

**Rollback criteria.** One lever: `pod.enabled=false` in config → the POD phase
is skipped entirely and the M5a digital path is bit-identical to today. No schema
to unwind. Revert the milestone if any of: (a) an enrichment run is observed to
change a SKU, property value or offering; (b) the provider→Etsy link exceeds
`link_timeout_seconds` on more than a quarter of builds; (c) the provider
publishes to Etsy `active` rather than `draft`; (d) any duplicate provider product
is created by an automatic retry; (e) the print-file host is found to expose
guessable or non-expiring URLs.

## 16. PRD §13 decision-log entries to append (42–49)

```
M5b design (2026-08-03; normative spec at
docs/designs/2026-08-03-m5b-pod-listings.md):

42. M5b = POD physical listings with TWO provider modes, because the providers
    differ in capability, not in preference. Gelato = mode "api_create": its
    ecommerce API creates the product and pushes the Etsy draft, so the path is
    unattended and sits behind the PodAdapter Protocol (adapters/pod/, httpx
    only, no SDK). Printful = mode "manual_adopt": its Products API "is not
    intended and will never support creating and managing products in external
    platforms", its Ecommerce Platform Sync API has no create endpoint, and API
    v2 dropped product management -- so a Printful Etsy listing can only be
    pushed from its dashboard, by hand. ShopSteward therefore discovers and
    ADOPTS the resulting Etsy draft and enriches it with the same code path
    (operator decision 2026-08-03). PodAdapter is not involved in manual_adopt.
    This is the ONE accepted exception to the three-gate rule and it is scoped
    to Printful physical listings: the hero, digital and Gelato paths are
    unaffected.
43. POD-first inversion: the provider creates the product AND the Etsy draft;
    ShopSteward then enriches ONLY title, description, tags and listing
    images. The POD path never calls createDraftListing, never calls
    updateListingInventory/update_listing_price, and never sends who_made,
    when_made, is_supply, taxonomy_id, state or quantity. Retail price is set
    inside the provider's product-creation call so the Etsy variation
    structure is authored once, by the provider, with our prices already in
    it. Gate 3 refuses a price edit on a POD draft in v1.
44. M5b reuses M5a's data model, not a parallel one (decision 37 realised):
    the listingdraft.* event namespace and proj_listing_drafts carry POD
    drafts with provider="gelato", sku_source="provider",
    listing_type="physical". POD config lives in a SEPARATE
    config/defaults/pod.json with its own pod_config_hash -- folding it into
    listing.json would change config_hash() and orphan every existing digital
    draft_id. Projections are drop-and-rebuild, so M5b needs no migration.
45. Variant selection is deterministic and human-free: photo aspect (from
    proj_landing_files width/height) -> pod.formats_by_aspect -> routing rules
    (ordered providers per product_type + region, first provider that stocks a
    matching variant; none -> the product type is dropped and recorded) ->
    minimum-DPI and max-price filters. Sizes and SKUs are a config decision
    made once, never a Gate 3 decision.
46. POD pricing: retail = max(unit_cost x markup, the closed-form price that
    satisfies the absolute margin floor, the closed-form price that satisfies
    the percentage margin floor), rounded to a configured price ending, capped
    by max_price (a variant priced above it is dropped). unit_cost = provider
    base cost + shipping estimate. The Etsy fee model is read from
    listing.json, not duplicated. compute_economics gains an optional
    unit_cost so Gate 3 shows price - fees - cost = net and margin %. Costs
    are config-supplied with a costs_verified_on date; past
    cost_staleness_days the Gate 3 card is flagged stale. No provider price
    API is called in v1.
47. Gate 3 becomes ONE CARD PER HERO carrying every listing that hero
    produced (digital + N physical); one tap emits gate3.hero_approved and
    publishes each listing. Per-draft publish is retained only for the failed
    drawer. This preserves the three-gate rule: a physical SKU adds no
    touchpoint.
48. Gelato fetches the print file from a URL -- it does not accept uploaded
    bytes. M5b therefore defines a PrintFileHost adapter
    (adapters/printfile/) with an offline fake; the real host is a NEW
    EXTERNAL SERVICE and an unresolved operator decision that gates only the
    live smoke test, not implementation. The signed URL is transient: only
    host name, file_key, expiry and sha256 are evented -- an append-only log
    must never hold a credential (decision 35's rule).
49. POD live-write safety: create_product is triple-gated (--live-pod +
    SHOPSTEWARD_LIVE_POD_WRITE=1 + provider credential + store id) and the
    print-file host separately gated; the adapter refuses any product the
    provider would publish live rather than as an Etsy draft; a create attempt
    with no confirmed product (pod_create_attempted without
    pod_product_created) is NEVER auto-retried -- it becomes state
    pod_unverified in the Gate 3 failed drawer for manual dashboard
    verification, because an automatic retry creates duplicate provider
    products and duplicate Etsy listings. Rollback is a single config flag,
    pod.enabled=false.
```

## 17. OPERATOR DECISIONS REQUIRED

Nothing below is assumed anywhere in the design. Q0–Q5 and Q7 block the live
path; none blocks slices 1–5 offline.

| # | Question | Answer form |
|---|---|---|
| **0** | *RESOLVED 2026-08-03 → **(b)**: Printful kept as a manual dashboard push + an "adopt an existing Etsy POD listing" enrichment path (§0, §13 slice 6). No action.* | — |
| **1** | *RESOLVED 2026-08-04: **Cloudflare R2**, activated on the operator's existing Cloudflare account. Free tier (10 GB, 1M Class A ops, zero egress, no card, no expiry) covers this permanently — the design revokes each print file after `pod_linked`, so storage never accumulates and ops stay in the dozens. B2 was rejected on a workload-specific detail: its free egress is 3× average stored data, which is ~0 for a delete-immediately pattern.* Env: `CLOUDFLARE_R2_KEY`, `CLOUDFLARE_R2_SECRET`, `CLOUDFLARE_R2_ENDPOINT`, **plus a bucket name (`CLOUDFLARE_R2_BUCKET`) still to be created**. `CLOUDFLARE_R2_TOKEN` is a Cloudflare management credential, NOT an S3 object credential — the code must not read it. | — |
| **1a** | *RESOLVED 2026-08-04: **`boto3`** approved as a dependency for SigV4 presigned URLs (S3-compatible against R2). Hand-rolled signing rejected — a subtle bug there mints URLs that never expire, publishing print masters permanently.* Bucket created and named by the operator; the code reads `CLOUDFLARE_R2_BUCKET` and must never read `CLOUDFLARE_R2_TOKEN` (a management credential, not an object credential). **Defence in depth:** an R2 Object Lifecycle Rule deletes objects after 1 day, so a print master cannot outlive its listing even if `revoke()` never runs. Public Development URL stays **disabled** — enabling it would put every print master on a permanent guessable URL and defeat §14 item 5. | — |
| **2** | Approve the new external services: Gelato API + Cloudflare R2 (Q1). Dependencies: `httpx` for Gelato (already declared) and **`boto3`** for R2 presigned URLs (approved, Q1a) — added in the slice that needs it, not slice 1. | yes / no |
| **3** | Which product types do we sell in v1? (framed_poster, poster, canvas, acrylic, metal — pick the set) | list |
| **4** | Which sizes per aspect? | list |
| **5** | Pricing knobs: `markup`, `margin_floor_abs`, `margin_floor_pct`, `max_price`, `price_ending`. | five values |
| **6** | Shipping included in the retail price (free shipping), or charged separately by the provider's profile? | included / charged |
| **7** | **Is Gelato configured to push new products to Etsy as DRAFTS?** If it publishes live, M5b's live path halts. Confirmed in the dashboard? | yes / no / not yet |
| **8** | Region: US only for v1? | yes / no |
| **9** | May enrichment **delete** provider-generated Etsy images to make room for our M4 mockups when the listing is at Etsy's 10-image cap? (`images.trim_provider_images`) | yes / no |
| **10** | Does `deleteListingImage` need the Etsy `listings_d` scope (which M5a's `delete_listing` also implies)? Already consented, or is another `shopsteward etsy auth` round authorised? | already have / re-consent OK / no |
| **11** | *Resolved from docs — Printful would be v1 sync-products; v2 has no product management. No action.* | — |
| **12** | *RESOLVED 2026-08-04 by a read-only API probe: **templates**, `create-from-template` is viable. Three corrections to this design came out of it, all of which would have failed live:* (a) **template variant ids are a different namespace from store-product variant ids** — the template's own `variants[].id` is what `templateVariantId` wants, and the two sets do not overlap; (b) **`placeholder` is NOT `"ImageFront"`** — Gelato names placeholders after the image file used when the template was built (e.g. `IMG_2485.jpg`), so it is per-template operator data, not a constant; (c) template variants carry **`fitMethod: null`**, so `slice` is ours to send, not something the template supplies. §6's block is illustrative; the shipped `config/defaults/pod.json` carries the real values. | — |
| **12a** | **NEW BLOCKER, poster tier only:** the operator's Fine Art Poster template has **two** image placeholders (`A60A7073.jpg`, `A60A7102.jpg`) and a photo-specific title. `PodCatalogVariant` models ONE placeholder per variant, so filling only the first would leave the template's original photograph composited into every automated poster. Recommended: build a new single-image poster template (the acrylic template is already that shape). Rejected: supporting multi-placeholder templates — a list plus a fill policy, for no benefit on a single-photo product. **Hold the poster tier out of any live run until resolved; acrylic is unaffected.** | rebuild / support-multi |
| **13** | Which landing artifact is the POD print file — the AdobeRGB TIFF master (re-encoded deterministically to max-quality sRGB JPEG) or the sRGB JPEG derivative? | tiff_master / jpeg |
| **14** | Is the Etsy shop's **production-partner declaration** in place (Etsy POD policy requirement)? | yes / no |
| **15** | Confirm: POD retail price is **not editable at Gate 3** in v1 (change `pod.json` markup and rebuild instead)? | yes / no |
| **16** | Approve the `pyproject.toml` import-linter amendment adding `shopsteward.adapters.pod` and `shopsteward.adapters.printfile` to both contracts? | yes / no |

## 18. Sources for §0

- Printful Products API / Ecommerce Platform Sync API: https://developers.printful.com/docs/
- Printful API v2 scope: https://developers.printful.com/docs/v2-beta/
- Printful help centre, adding products to Etsy: https://help.printful.com/hc/en-us/articles/25186194818204
- Gelato create-from-template: https://dashboard.gelato.com/docs/ecommerce/products/create-from-template/
- Gelato guide: https://dashboard.gelato.com/docs/guides/create-product-from-template/
