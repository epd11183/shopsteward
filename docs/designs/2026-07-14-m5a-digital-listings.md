# M5a Design -- Digital-Direct Etsy Listings + Gate 3 Publish

*Architect output 2026-07-14. Operator-decided constraints encoded below as
PRD 13 decision candidates 37-41 (paste block in 12); do not relitigate.
Splits PRD milestone M5: M5a = digital-direct Etsy listings (this doc);
M5b = POD (Gelato/Printful) enrichment (later). Digital listings are created
directly via the Etsy API (5.4 "Digital listings: created directly").*

Extends house patterns exactly: append-only core.events, drop-and-rebuild
module-local projections with a rebuild_*() entrypoint, Protocol adapters with
fixture/fake defaults, on-demand runs (no daemons), Typer sub-apps, APIRouter
mounted by top-level api.py, config loaded file-direct then seeded to the DB
via events (tuning-profile precedent). Consumes proj_landing_files (status
valid) + proj_mockups/proj_mockup_sets (M4) + proj_scores vision verdict (M3).
Produces Etsy drafts + Gate 3. No POD, no bundles, no IG.

## 1. Dataflow

```
landing original (proj_landing_files) --+
mockup set (proj_mockups, M4) ----------+--> listings build (OFFLINE, unattended)
proj_scores vision verdict (M3) --------+       |
config/defaults/listing.json (DB-seeded) ------+
                                               v
   [local draft] copy(CopyAdapter) + price(pricing.py, floor) + image order(images.py)
   listingdraft.created / .copy_generated / .priced / .images_selected   (+ llm.call)
                                               |
                                               v  push (EtsyWriteAdapter: Fake default / Live triple-gated)
   create_draft_listing(type=download) -> upload_listing_image[] -> upload_listing_file -> update_listing
   listingdraft.pushed_to_etsy / .images_attached / .file_attached   (state=DRAFT on Etsy)
                                               |
                                               v
                         +--------- GATE 3 (React) ----------+
                         | default-accept, ONE tap publish   |
                         | edit title/tags/desc/price (opt)  |  <- price override < floor REJECTED
                         | shows economics: price - fees     |
                         +----------------+------------------+
                                          v  publish (Gate 3 endpoint ONLY; triple-gated)
                               publish_listing() -> state=ACTIVE
                               gate3.approved / gate3.published (| .publish_failed)
```

The sellable file is the landing original, never a mockup. Mockup-set images
become the listing images, hero first. AI (copy + vision) never touches the
photograph; the disclosure line is appended to any description whose listing
carries room mockups.

## 2. Module / file map

listings/ is a package (not listings.py): it spans copy generation, pricing,
image ordering, draft orchestration, push, Gate 3, projections, API, CLI --
the same concern-count that made mockups/ a package. Lives under pipeline/ so
import-linter contracts are unchanged (editing stays clean).

```
src/shopsteward/pipeline/listings/__init__.py
src/shopsteward/pipeline/listings/models.py    Pydantic v2: ListingConfig, PricingRules, CopyInputs,
                                               ListingDraftSpec, Economics, Gate3Card, BuildReport.
src/shopsteward/pipeline/listings/config.py    LISTING_CONFIG_PATH + load_listing_config() + config_hash();
                                               seed()->listingconfig.seeded; get_config() reads projection.
                                               (tuning.py precedent, file-direct + event seed.)
src/shopsteward/pipeline/listings/copy.py      generate_copy(): build CopyAdapter, inject house style guide +
                                               vision verdict, ONE structured-JSON call, append llm.call,
                                               append disclosure line. Soft-cap check before the live call.
src/shopsteward/pipeline/listings/pricing.py   PURE: apply_price(format,rules)->price; enforce_floor(price,rules)
                                               ->raises BelowFloor; compute_economics(price,rules)->Economics.
src/shopsteward/pipeline/listings/images.py    PURE: order_listing_images(mockups, config)->[(path,intent,rank)]
                                               (hero single first, whatyougot included, capped at 10);
                                               resolve_sellable_file(landing_row)->(bytes, source, sha256).
src/shopsteward/pipeline/listings/drafts.py    Orchestrator: eligible landing files -> build local draft ->
                                               push via EtsyWriteAdapter. Idempotent by draft_id. -> BuildReport.
src/shopsteward/pipeline/listings/gate3.py     Queue read (Gate3Card[]); edit(draft, fields) w/ floor enforce +
                                               update_listing; publish(draft)->publish_listing + gate3 events.
src/shopsteward/pipeline/listings/projections.py  proj_listing_config, proj_listing_drafts; rebuild_listings().
src/shopsteward/pipeline/listings/api.py       APIRouter /api/pipeline (listings + gate3 routes); mounted by api.py.
src/shopsteward/pipeline/listings/cli.py       shopsteward listings build|status. No gate3 CLI (UI is the
                                               decision surface, mirroring Gate 1).
src/shopsteward/adapters/etsy/interface.py     + EtsyWriteAdapter Protocol (4).
src/shopsteward/adapters/etsy/live.py          LiveEtsyAdapter grows the write methods (wired to EtsyTokenAuth).
src/shopsteward/adapters/etsy/fake.py          + FakeEtsyWriteAdapter (in-memory twin, tests + offline default).
src/shopsteward/adapters/copy/interface.py     CopyAdapter Protocol + CopyVerdict/CopyResult/CopyUsage/CopyParseError.
src/shopsteward/adapters/copy/openrouter.py    OpenRouterCopyAdapter (httpx only; strict json_schema, temp from cfg).
src/shopsteward/adapters/copy/fake.py          FixtureCopyAdapter (default) + FakeCopyAdapter (programmable, tests).
config/defaults/listing.json                   schema shopsteward.listing/1: copy + pricing + image_order + etsy.
config/defaults/prompts/listing_copy.txt       Copy prompt template (config-over-code, COMMERCIAL_PROMPT_PATH twin).
config/defaults/prompts/house_style.md         House voice/style guide text, injected into every copy prompt.
frontend/src/pages/Gate3.tsx                   Default-accept publish card; App.tsx "Gate 3" tab.
tests/pipeline/listings/... + tests/adapters/etsy|copy/...   fakes + fixtures (10).
```

import-linter: add shopsteward.adapters.copy to the editing-standalone
contract forbidden_modules, and to the "pipeline imported by no lower layer"
contract source_modules. adapters.etsy is already forbidden to editing. No
other contract changes -- listings is inside pipeline.

## 3. Events, projections, idempotency

Dot-separated, past tense, immutable, user_id on every row. llm.call is the
existing ledger (M3) -- copy adds purpose:"listing_copy".

| Event | Payload |
|---|---|
| `listingconfig.seeded` / `.updated` | `{name:"default", config:{...}, source:"defaults"\|"operator"}` |
| `listingdraft.created` | `{draft_id, landing_file_id, photo_id\|null, set_key, provider:"etsy_digital", format:"digital_download", sku_source:"etsy", listing_type:"download", config_hash}` |
| `listingdraft.copy_generated` | `{draft_id, title, tags:[<=13], description, materials?, model, provider, disclosure_appended:bool}` |
| `listingdraft.priced` | `{draft_id, format, base_price, margin_floor, price, currency, auto:true}` |
| `listingdraft.images_selected` | `{draft_id, images:[{path, intent, rank}], sellable_file:{source:"landing_original"\|"derived_jpeg", sha256, bytes}}` |
| `llm.call` | `{provider, model, purpose:"listing_copy", draft_id, input_tokens, output_tokens, est_cost_usd}` -- LIVE copy only |
| `listingdraft.pushed_to_etsy` | `{draft_id, etsy_listing_id, listing_type:"download", quantity, state:"draft"}` |
| `listingdraft.images_attached` | `{draft_id, etsy_listing_id, images:[{etsy_image_id, rank, intent}]}` |
| `listingdraft.file_attached` | `{draft_id, etsy_listing_id, etsy_file_id, source, sha256}` |
| `listingdraft.push_failed` | `{draft_id, etsy_listing_id?, stage:"create"\|"image"\|"file"\|"update", error:{code,message}}` |
| `listingdraft.edited` | `{draft_id, etsy_listing_id, fields:{title?,tags?,description?}, price?}` -- Gate 3 operator edit; floor-checked |
| `gate3.approved` | `{draft_id, etsy_listing_id, final_price, currency}` -- operator tapped publish |
| `gate3.published` | `{draft_id, etsy_listing_id, state:"active", published_at}` |
| `gate3.publish_failed` | `{draft_id, etsy_listing_id, error:{code,message}}` |

Projections (rebuild_listings, drop/rebuild, user_id everywhere):
- proj_listing_config(user_id, name PK, config_json) last-write-wins.
- proj_listing_drafts(user_id, draft_id PK, landing_file_id, photo_id, provider,
  format, sku_source, etsy_listing_id NULL, listing_type, title, tags_json,
  description, price, currency, margin_floor, images_json, file_source,
  state built|pushed|published|push_failed|publish_failed, created_at,
  published_at NULL). .edited/.priced fold updates; gate3.published advances
  state.

Idempotency: draft_id = sha256(landing_file_id | config_hash | set_key)
(set_key = the M4 mockup set). Skip a landing file whose draft_id is already
published; --force rebuilds copy/price/images (never re-publishes). Push is
skipped when the draft already carries an etsy_listing_id (re-push =
update_listing, not a second create). Ownership rule holds: listings never
writes proj_photos, proj_landing_files, or proj_mockups.

## 4. Adapter interface additions

adapters/etsy/interface.py -- a separate write Protocol (the read adapter is
untouched; the fixture read adapter needs no write methods):

```python
class EtsyWriteAdapter(Protocol):
    def create_draft_listing(self, spec: EtsyDraftSpec) -> EtsyListingRef: ...
    def upload_listing_image(self, listing_id: int, image: bytes, *, rank: int) -> EtsyImageRef: ...
    def upload_listing_file(self, listing_id: int, file: bytes, *, name: str, rank: int) -> EtsyFileRef: ...
    def update_listing(self, listing_id: int, fields: EtsyListingUpdate) -> EtsyListing: ...
    def publish_listing(self, listing_id: int) -> EtsyListing: ...   # state=active
    def delete_listing(self, listing_id: int) -> None: ...           # smoke-test cleanup ONLY
```

EtsyDraftSpec (models.py): quantity, title, description, price{amount, divisor,
currency}, who_made, when_made, taxonomy_id, type="download", is_supply=False,
tags[], should_auto_renew. Digital listings set no shipping profile.
create_draft_listing always creates state=draft -- no adapter method activates
except publish_listing, and that is called only by the Gate 3 endpoint.

adapters/copy/interface.py:

```python
class CopyVerdict(BaseModel):
    title: str = Field(max_length=140)
    tags: list[str] = Field(max_length=13)          # each <=20 chars (schema)
    description: str
    materials: list[str] | None = None

class CopyResult(BaseModel):
    verdict: CopyVerdict
    usage: CopyUsage | None = None                  # None => fixture, no llm.call

class CopyAdapter(Protocol):
    def generate_copy(self, inputs: CopyInputs, *, model: str) -> CopyResult: ...
```

OpenRouterCopyAdapter mirrors OpenRouterVisionAdapter exactly (httpx,
response_format=json_schema strict, Bearer key, HTTP-Referer/X-Title). Strict
schema: title maxLength 140, tags maxItems 13 items maxLength 20, description
string. Parse failure -> CopyParseError -> listingdraft not created; never
guessed copy.

## 5. Copy-generation contract

One call per draft. CopyInputs = house style guide (from house_style.md) +
per-photo signals read from proj_scores: subject, strongest_room_style,
one_risk, rationale; plus orientation, format="digital_download", digital
sizes[] + formats[] (from mockups.json whatyougot). Prompt template
(listing_copy.txt) frames the task and enumerates Etsy limits; the strict
schema enforces them. After generation, copy.py appends mockups.json
listing_copy.ai_disclosure_line (config mockups.json is the single source of
the line, per M4) to description whenever the listing carries room mockups --
always true for M5a -- and sets disclosure_appended=true (gated on listing.json
copy.append_disclosure). temperature from config (default 0.4). Cost logged to
llm.call; live copy refused past the shared vision.monthly_soft_cap_usd $10/mo
(now covers vision AND copy).

## 6. config/defaults/listing.json (schema shopsteward.listing/1)

```json
{ "schema": "shopsteward.listing/1", "name": "default",
  "copy": {
    "provider": "openrouter",
    "model": "anthropic/claude-sonnet-5",
    "ab_alternate": "deepseek/deepseek-chat",
    "temperature": 0.4,
    "append_disclosure": true,
    "prompt_path": "prompts/listing_copy.txt",
    "house_style_path": "prompts/house_style.md",
    "est_cost_per_mtok": { "anthropic/claude-sonnet-5": {"in": 2.00, "out": 10.00},
                           "deepseek/deepseek-chat": {"in": 0.27, "out": 1.10} } },
  "pricing": {
    "currency": "USD",
    "digital_quantity": 999,
    "formats": { "digital_download": {"base_price": 12.00, "margin_floor": 6.00} },
    "etsy_fees": {"listing_fee": 0.20, "transaction_pct": 0.065,
                  "payment_pct": 0.03, "payment_flat": 0.25} },
  "image_order": ["single", "framed_poster", "canvas_edge", "acrylic",
                  "gallery_wall", "digital_whatyougot"],
  "image_cap": 10,
  "etsy": {
    "who_made": "i_did", "when_made": "2020_2025", "is_supply": false,
    "taxonomy_id": 0, "should_auto_renew": true, "sellable_max_bytes": 20000000 } }
```

config_hash() = sha256 of canonical compact JSON (M4 precedent). image_order
ranks mockup intents into Etsy image slots -- hero single first (rank 1 = Etsy
primary), digital_whatyougot panel included, capped at image_cap. taxonomy_id
and Etsy enum strings are verified at fixture-recording (like the vision
REST-shape uncertainties, M3 4).

## 7. Digital-listing mechanics

- Type / quantity. createDraftListing type="download", no shipping profile.
  Digital quantity does not decrement per sale; set from
  pricing.digital_quantity (default 999). Etsy requires >=1 image AND >=1
  digital file before a draft can be published.
- Sellable file = the landing original (proj_landing_files.path for this
  landing_file_id), uploaded via upload_listing_file. If it is a TIFF or
  exceeds etsy.sellable_max_bytes (Etsy 20 MB/file limit), images.py produces a
  deterministic max-quality sRGB JPEG (Pillow re-encode, no resize beyond the
  limit, no AI) and records source="derived_jpeg". The artwork is never
  AI-touched; a re-encode is deterministic image processing.
- Listing images = the M4 mockup set for this landing file, ordered by
  image_order, uploaded via upload_listing_image(rank=...). Mockups are
  presentation only -- the buyer downloads the original, not a mockup.
- Flow order (all-or-fail per stage, push_failed carries the stage): create
  draft -> upload images in rank order -> upload sellable file -> update draft
  with copy+price. Draft sits on Etsy in state=draft until Gate 3.

## 8. Gate 3 -- API + UI

API /api/pipeline:
- POST /listings/build {photo_id?, live_copy?:false, live_etsy_write?:false}
  -> BuildReport {drafts_built, pushed, copy_calls, skipped_idempotent,
  push_failed} (403 unless the relevant gate is open when a live flag is set).
- GET /gate3/queue -> Gate3Card[] {draft_id, etsy_listing_id, title, tags,
  description, price, currency, margin_floor, economics:{price, etsy_fees,
  net}, images:[{url, rank, intent}], state}.
- GET /gate3/draft/{draft_id}/image?path= -> FileResponse, validated under
  mockups_dir() (no traversal -- M4 precedent).
- POST /gate3/edit {draft_id, title?, tags?, description?, price?} -> updates
  the draft + update_listing; price below margin_floor -> 400 (enforced
  server-side); emits listingdraft.edited/.priced.
- POST /gate3/publish {draft_id} -> the ONLY publish path; triple-gated;
  publish_listing -> gate3.approved + gate3.published (| .publish_failed).

UI Gate3.tsx (Gate1.tsx conventions -- single card, keyboard-first): mockup
carousel (hero first), editable title/tags/description, price field with live
economics strip (price - Etsy fees = net), floor guard (publish disabled +
inline reason if price < floor), one "Publish" button (default-accept: nothing
is required to edit). Post-publish inline "live on Etsy -> listing_id" chip.
Snoozed/failed drawer for push_failed/publish_failed retry.

## 9. Live-gating (all default OFF; 8.4)

Two independent AI/write gates, each triple-checked, mirroring vision.

| Path | Offline default | Live requires |
|---|---|---|
| copy generation | FixtureCopyAdapter (usage None -> no llm.call) | --live-copy + SHOPSTEWARD_LIVE_COPY=1 + OPENROUTER_API_KEY + under soft cap |
| Etsy draft push | FakeEtsyWriteAdapter (in-memory) | --live-etsy-write + SHOPSTEWARD_LIVE_ETSY_WRITE=1 + tokens on disk (listings_w scope) |
| Gate 3 publish | FakeEtsyWriteAdapter | same as Etsy draft push |

live_gate.py grows live_copy_open()/..._error() (keyed on OPENROUTER_API_KEY)
and live_etsy_write_open()/..._error() (checks env + EtsyTokenStore.load()
present AND listings_w in tokens.scopes). Adapters ONLY create/update drafts;
publish_listing is invoked solely by /gate3/publish. Write re-consent:
listings_w is added to the Etsy scopes -- operator runs shopsteward etsy auth
once more. auth.py DEFAULT_SCOPES must be widened to include listings_w (the M3
allowlist was read-only listings_r transactions_r shops_r); this is a 8.2
secrets/scope change and a noted follow-up, not a redesign.

## 10. Test plan (all offline, zero network)

- FakeEtsyWriteAdapter: in-memory dict; assigns monotonic
  listing_id/image_id/file_id; tracks state; publish_listing flips
  draft->active; enforces ">=1 image + >=1 file before publish" so the contract
  is exercised; delete_listing removes.
- FixtureCopyAdapter: verdicts keyed by subject/photo from
  tests/fixtures/copy/*.json, deterministic pseudo-copy for unknowns; usage
  None -> no llm.call. FakeCopyAdapter: programmable queue (results + usage +
  exceptions) for ledger + parse-failure + soft-cap tests.
- Write fixtures: request/response JSON shaped from Etsy v3 docs now
  (createDraftListing, uploadListingImage/File multipart responses,
  updateListing, publish), replaced by recorded + scrubbed real ones after the
  operator-approved 8.4 smoke: create ONE draft on the real shop with
  LiveEtsyAdapter, verify fields, delete_listing it. Never commit live ids.
- E2E (tests/pipeline/listings/test_e2e_gate3.py): seed a landing file + its M4
  mockup set + a proj_scores verdict -> listings build with Fake copy + Fake
  Etsy -> assert listingdraft.created/.copy_generated/.priced/.images_selected
  then .pushed_to_etsy/.images_attached/.file_attached; assert the disclosure
  line is appended to description; assert hero single is rank 1 and
  digital_whatyougot is present; assert the sellable file is the landing
  original (or derived_jpeg for a TIFF fixture), never a mockup path. Gate 3:
  edit price below floor -> 400; valid publish -> gate3.published, state active
  on the fake. Re-build -> skipped_idempotent. Assert zero llm.call events and
  zero network. Assert live-flag-without-env -> the triple-gate refusal (both
  copy and etsy_write).

## 11. Non-goals (explicit)

POD / Gelato / Printful enrichment (M5b -- the data model already carries
provider/sku_source so 5b slots in without migration); bundle proposer (catalog
too small); physical variation matrices; Instagram (M6); Stage 5 listing
management (post-publish edits, renewals, inventory, receipts -> feedback);
multi-currency beyond the configured one; multi-tenant Etsy accounts (single
user_id v1).

## 12. PRD 13 decision-log amendment (candidates 37-41)

```
37. M5 splits: M5a = digital-direct Etsy listings (create-as-DRAFT via the
    Etsy API); M5b = POD (Gelato/Printful) enrichment, deferred. The listing
    data model carries provider + sku_source (M5a = etsy_digital / etsy) so
    the POD-first enrichment path slots into M5b without migration. M5a builds
    nothing POD-specific.
38. Listing copy = one structured-JSON call per draft (title/tags/description,
    strict schema like the vision adapter) via OpenRouter (decision 36
    transport), default model anthropic/claude-sonnet-5, DB-configured
    (model id + prompt template + house style-guide text in config/DB, never
    hardcoded), A/B-able vs deepseek/deepseek-chat. Cost logged to the llm.call
    ledger under the shared $10/mo soft cap (now covers vision + copy).
39. Pricing = DB-seeded per-format base price + margin floor, auto-applied to
    drafts. Gate 3 shows the price breakdown and economics (price - Etsy fees)
    and allows override; an override below the floor is rejected server-side.
40. Gate 3 = default-accept: a drafted listing set per hero is publishable with
    ONE tap; title/tags/description/price/images are editable but never
    required. Bundle proposer is OUT of scope for M5a (catalog too small).
41. Etsy write safety: adapters may only create/update DRAFT listings; the sole
    publish path is the Gate 3 endpoint; live Etsy writes are triple-gated
    (--live-etsy-write + SHOPSTEWARD_LIVE_ETSY_WRITE=1 + tokens present, and
    live copy triple-gated on OPENROUTER_API_KEY); publishing shows computed
    economics before the tap; the AI-disclosure line (mockups.json
    listing_copy.ai_disclosure_line) is appended to every generated description
    that includes room mockups; write re-consent adds ONLY the listings_w scope
    (operator re-runs etsy auth; auth.py DEFAULT_SCOPES widened for M5).
```

## 13. Rejected alternatives

- Single EtsyAdapter with read+write methods -- forces the read-only fixture
  adapter to stub writes; a separate EtsyWriteAdapter Protocol keeps the read
  path (M1) and its fixtures untouched.
- listings.py single module -- the copy/price/image/push/Gate3/projection
  concern-count matches mockups/; a flat module would be one 600-line file.
- Push to Etsy only at the Gate 3 tap -- then the operator cannot preview a
  real draft and the "one tap" would do create+upload+publish under one live
  gate; splitting build/push (draft) from publish keeps prep unattended and the
  tap a single state flip.
- Copy folded into the vision adapter -- different provider model, schema, and
  purpose; a separate adapters/copy/ keeps each strict schema clean and
  A/B-able independently.
- Uploading a mockup as the sellable file -- the buyer must receive the
  artwork, not a room scene; sellable = landing original (or deterministic sRGB
  JPEG derivation).
- Pricing/copy config hardcoded in Python -- violates config-over-code; seeded
  from config/defaults/listing.json via listingconfig.seeded.
