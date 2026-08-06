# ShopSteward — pick-up-here

*Last updated 2026-08-05. Written at the end of a long session; the value here is
the decisions and the landmines, not the task list.*

---

## Start here (highest value first)

### 1. Build the two Gelato **canvas** templates — landscape and portrait
Canvas is **77% of your revenue** (2 of 7 orders, $188.67 of $245.88) and it is
the only physical product you have ever sold. Acrylic and poster — the two we
automated first — have **never sold a unit**.

`config/defaults/pod.json` already carries canvas with real costs
(12x18 $25.39 / 16x24 $28.69 / 20x30 $51.79, slim FSC wood) and real prices
(89 / 109 / 169). It needs two things only you can supply:

- `template_id` for each of the two templates
- `variant_key` per size — **the template's own `variants[].id`, NOT the store
  product's variant id.** Those are different namespaces and using the wrong one
  fails every create call. (This bit us once already.)

Two templates because a Gelato template has a single orientation, and canvas is
the **only** product you have that Gelato offers vertically. Portrait
photographs currently drop with reason `orientation` and have nowhere to go.

Then run the probe — it discovers everything and prints no secrets:
```
GELATO_API_KEY=... uv run --with httpx --no-project python \
  <scratchpad>/probe_gelato.py
```

### 2. Order one slim Gelato canvas of a photo you have already sold
~$25. The **only** open question I could not answer with data. Gelato slim is
cheaper than Printful at both comparable sizes ($25.39 vs $26.75; $28.69 vs
$33.25) — your memory that Printful was cheaper was accurate but keyed to
Gelato's *thick* canvas, which is $34–37. Cost is settled; whether the product
is good enough to carry your name is not, and you have sold Printful canvas twice.

### 3. Lightroom `CropAngle` spike (M2b, ~10 minutes)
Apply `CropAngle = 1.5` to one frame via the existing manual "Apply Settings"
command, run the export report, read it back. Decides whether horizon levelling
is buildable at all. If `applyDevelopSettings` discards geometry keys, levelling
is cut from M2b v1 — see `docs/designs/2026-08-03-corrections-proposer.md` §9.

⚠ **Expect it to fail.** Adobe's forums say `applyDevelopSettings` is undocumented
and that crop support is not in the documented SDK. Fallback is a CRS XMP sidecar
written with stdlib `ElementTree` — no new dependency. **Test the round-trip, not
just the write.** See `docs/research/2026-08-05-external-tools-survey.md` §0 and §3.2.

### 4. Run `shopsteward sync --live` and then `shopsteward ops brief`
Live sync is merged and gated (`--live` + `SHOPSTEWARD_LIVE_ETSY_READ=1` +
tokens). It is **read-only** — `get_shop`, `list_listings`, `list_receipts`.

Expect: **sales history comes back retroactively** (receipts go back years), but
**views/favourites do not** — those are point-in-time, so the daily time series
starts the day you first sync and builds forward. Conversion analysis needs a
few weeks of accumulation.

⚠ `list_listings` now fetches active **+ expired** via a `state` filter. That
parameter shape came from Etsy's docs, not a live call — **your first `--live`
run is its verification.** One-line revert if `state=expired` 400s.

### 5. Check M5a's digital path for the same duplicate bug
The only known place the per-file-iteration defect may still live. Slice 2 fixed
it for physical products; the digital path was never checked. Cheaper to look now
than to find it as two identical $7.99 downloads in your shop. Grep the digital
draft builder for `landing_file_id` keying and compare against
`pipeline/listings/pod/build.py:_photo_key`.

---

## State of the code

| | Where | Status |
|---|---|---|
| M5b slice 1 (POD adapter, config, selection) | `main` | merged |
| Real Gelato catalog + pricing policy | `main` | merged |
| Gelato canvas, landscape + portrait | `main` | merged |
| Live read-only Etsy sync | `main` | merged |
| M8a slice 1 (shop brief) | `main` | merged |
| M5b slice 2 (pricing, R2 host, build) | `main` | merged |
| External tools survey | `main` | merged |

**`main` is pushed** — `e3fbb4f`, `origin/main` in sync, 566 tests, `ruff` clean.
The repo is public and **MIT licensed**, and is going open source properly.

### The slice 2 defect, and why its tests are trustworthy
**The defect was in the SPEC, not just the code.** `draft_id` keyed on
`landing_file_id` while `scan_landing` writes **one row per file** — and your
Gate 2 export produces *both* a TIFF master and an sRGB JPEG. One photograph
became two drafts, two Gelato products, and **two live Etsy listings of the same
image at $89–229 each.** Now keyed on `photo_id`, falling back to
`landing_file_id` for unmatched manual drops. Design §3 corrected 2026-08-04.

**The guard tests are mutation-verified.** A mutation was left in the working
tree (`_photo_key` reverted to `file_id`) and the suite caught it —
`test_photo_landed_as_tiff_and_jpeg_produces_one_draft_per_product_type` and
`test_draft_id_is_stable_regardless_of_which_sibling_is_encountered_first` both
failed, 6 drafts where 3 were expected. They fail for the right reason, not
vacuously. **If you ever touch `_photo_key`, those two tests are the alarm.**

⚠ **No review verdict was ever produced.** One was queued overnight on
2026-08-04; it left no document. `review-receipts/` is protect-mcp audit output,
not a review. Slice 2 was merged on your instruction with tests and lint green,
**not on a reviewer's sign-off** — so it has had less scrutiny than the rest of
`main`. Worth a read-through when convenient.

⚠ **M5a's digital path shares the same per-file iteration and has NOT been
checked.** Its blast radius is a duplicate digital listing rather than a
duplicate physical product, but it is the same bug — and now that slice 2 has
shipped the fix on the physical side, this is the last place it lives.

---

## Decisions made 2026-08-04 (so they are not relitigated)

- **Printful is out.** Not preference — its API cannot create a product or a
  listing. Verified by enumeration: their official Postman collection has 89
  endpoints and **zero** product-creation endpoints. It is an order-fulfilment
  API. Closed; see M5b design §0a. Do not research a fourth time.
- **Gelato is the sole supplier**, canvas included.
- **Product ladder:** acrylic premium, poster entry, canvas the proven middle.
- **Pricing:** cost-plus markup is the automatic *guard*; explicit per-variant
  prices win where the supplier's cost curve does not track value. Acrylics keep
  your existing live prices (shop-wide consistency); posters and canvas get value
  ladders. All 11 SKUs clear both margin floors at 53–64%.
- **Budget:** $20/month, 100% to listing fees, **0% to ads** for the first three
  months. At 2.1% conversion, ads against a $33 order are break-even; against a
  $107 canvas they are ~6x. Publish the catalog first, then advertise.
- **Print-file host:** Cloudflare R2, free tier, `boto3` for presigned URLs.
  Lifecycle rule deletes objects after 1 day so a print master cannot outlive
  its listing even if `revoke()` never runs.
- **Orientation is part of variant selection.** Was not, and a portrait hero
  would have been centre-cropped into a landscape SKU.

## Shop reality (from your Etsy dashboard + order detail)

- All-time: 1,754 views → 1,092 visits → 10 orders → **$370**
- This year: 635 views (+120% YoY), 7 orders (+600%), $230 (+255%)
- **Conversion is 2.1% this year, up from 0.39%** — that is healthy, not weak.
  Traffic is the binding constraint, not conversion.
- **Digital downloads: 71% of orders, 23% of revenue.** Already automated (M5a).
- **Canvas: 29% of orders, 77% of revenue.** Was not automated. Now is.
- Subjects that sell: wildlife and national parks. Osprey, bison, black bear cub,
  Grand Teton, Milky Way. The black bear cub sold **both** as a $7.99 download
  and a $107.67 canvas — same subject, both ends of the ladder.
- ⚠ **100 products in Gelato vs 27 active Etsy listings.** Worth understanding —
  if most are unpublished, the bottleneck is publishing, not creating.

## Landmines

- **Template variant ids ≠ store-product variant ids.** Different namespaces.
- **`placeholder` is not `"ImageFront"`** — Gelato names it after the image file
  used when the template was built (yours is `IMG_2485.jpg`). Per-template
  operator data, not a constant.
- **Config was write-once** until `pod config apply` / `ops config apply` existed.
  Editing a JSON file after seeding did nothing — the documented kill switch, the
  price remedy, and the pre-smoke cost population were all silent no-ops.
- **`EtsyTransaction` carries no variation or SKU**, so *which size sold* is not
  derivable. The brief parses it from titles, best-effort, and says so. Fix
  forward: M5b creates the listings, so the title convention can carry the size.
  Your existing titles already encode aspect ratio as `(2:3)`.
- **`data/` and `.env*` are off limits to the agent.** Some commands here are
  written for you to run for that reason.
- **`proj_listings` discards history** (`INSERT OR REPLACE`); `proj_listing_daily`
  is what preserves it.

## Open questions

- Gelato slim vs thick canvas — a product judgement, not a cost one (#2 above).
- Whether the ~73 Gelato products absent from Etsy are unpublished or deleted.
- M8 autonomy framework: 17 operator decisions, none answered. Draft only, and
  explicitly not a proposal to build —
  `docs/designs/2026-08-03-m8-autonomous-operations-draft.md`.
- M2a RAW-only ingest: 13 decisions, spec complete, unstarted.
  RAW-only folders are currently **invisible to the entire system**.

## Research

- `docs/research/2026-08-05-external-tools-survey.md` — culling, Lightroom
  automation, presets, POD. Verdicts are BORROW / REFERENCE / **CLOSED**; the
  CLOSED list exists so we stop re-researching Printful, Lightroom cloud, and
  the two non-commercial Jarvis models. Lands in M2a/M2b/M3, not M5b.

## Designs written but not built

- `docs/designs/2026-08-03-raw-only-ingest.md` — M2a
- `docs/designs/2026-08-03-corrections-proposer.md` — M2b (WB/exposure/levelling)
- `docs/designs/2026-08-03-m8-autonomous-operations-draft.md` — M8 (DRAFT)
