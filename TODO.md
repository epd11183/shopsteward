# ShopSteward — pick-up-here

*Last updated 2026-08-06. Written at the end of a long session; the value here is
the decisions and the landmines, not the task list.*

---

## Start here (highest value first)

### 1. Resume Alaska batch scoring, in chunks of ~20
`uv run shopsteward score run --live-vision --limit 20`, repeat. **114 of 380
scored, 266 remaining.** The scoring pipeline was recalibrated today (see
below) and is now live-verified working correctly — real score spread, no
false positives, appropriately strict.

⚠ **Cost/speed tradeoff, now accurate**: the widened escalation band means
most photos hit Pro, averaging **~20s/photo** (real measured average across
three live batches today, not an estimate). At that rate the remaining 266
photos are **~1.5 hours** of wall-clock + API cost. Real spend so far:
**~$1.63 total** across the whole batch-to-date (soft cap is $10/month).

Gate 1 UI: `uv run shopsteward serve` (rebuild frontend first if
`frontend/dist` is stale relative to `frontend/src` — `npm run build` in
`frontend/`), then http://127.0.0.1:8321/. ⚠ Photos queue **incrementally**
as a batch runs, not all at once — if you check Gate 1 mid-batch you'll see
fewer candidates than the batch eventually produces. Not a bug; confirmed by
tracing DB events directly. If Gate 1 looks stale after a batch finishes,
hard-refresh the tab (Ctrl+Shift+R) before assuming anything's wrong.

**Current Gate 1 tally: 12 approved, 12 rejected, 0 snoozed** (all 24
queued-so-far candidates have been judged). Bears continue to perform best
(matches historical sales data — the black bear cub is the proven top
seller); eagles and whale-tail/documentary-style shots underperform.

### 2. Build the two Gelato **canvas** templates — landscape and portrait
Untouched this session. Still the single highest-value open item — canvas is
**77% of your revenue** (2 of 7 orders, $188.67 of $245.88) and it is the
only physical product you have ever sold. See "Decisions made 2026-08-04"
below for the full context; nothing here has changed.

### 3. Order one slim Gelato canvas of a photo you have already sold
Also untouched. ~$25, the one open question data can't answer.

### 4. Lightroom `CropAngle` spike (M2b, ~10 minutes)
Untouched. See "Decisions made 2026-08-04" below.

### 5. Check M5a's digital path for the same duplicate bug
Untouched.

### 6. Pinterest traffic-source check
Untouched — still needs a manual look at Etsy Stats → Traffic Sources.

---

## What happened today (2026-08-06)

Two PRs merged to `main`: **#18** (vision-scoring rationale-cap fix + M6 Meta
adapter slice 1, from the prior session) and **#19** (scoring recalibration,
this session). Both went through the full flow: local commit → feature
branch → push → PR → merge, `main` and `origin/main` in sync, working tree
clean.

### The scoring pipeline was fundamentally miscalibrated, and now measurably isn't
Real Gate 1 curation on the original 12 queued Alaska photos came back 6
rejected — the operator's gut said "the AI's bar is too loose," and it was
provably right, not just a feeling:

- **The commercial-scoring prompt had zero score anchors** — `commercial_score`
  was a bare "integer 0-100." Measured directly: the triage model
  (`gemini-2.5-flash-lite`) had collapsed to **3 distinct values (45/65/75)
  across 47 real photos**. Not an estimate — counted from real event data.
- **Fix**: rewrote `config/defaults/prompts/commercial_score.txt` with
  explicit score-band anchors grounded in the shop's actual sales history
  (the black bear cub — sold twice, sets the ~90+ bar), a "portfolio-quality
  vs shop-quality" distinction (the operator's own framing, adopted
  verbatim), and an explicit instruction against round-number/tied defaults.
  Verified: distinct triage values roughly doubled, distribution shifted
  down to match real curation outcomes.
- **The prompt fix alone didn't fix the top end.** Photos scoring just above
  the old `borderline_band` (10, window [50,70]) were skipping Pro escalation
  entirely and relying on the cheap triage model's coarse judgment — which is
  exactly where the tied/clustered scores were worst. Escalating those
  specific photos to Pro with the new prompt produced a real 30-point spread
  (42-72) and a *consistent, specific diagnosis* across every one of them:
  shooting distance/intimacy, not subject choice, is the actual problem.
- **Fix**: `scoring.borderline_band` 10 → 15 (window now [45,75]), applied
  via a one-off script appending a `tuningprofile.updated` event directly.
  ⚠ **There is no `tuning apply` CLI command** — same gap `pod`/`ops config
  apply` were in before they existed. If tuning-profile changes become a
  recurring need, that's real (small) infra worth building; for now, editing
  `config/defaults/tuning_profile.json` alone **does nothing** once a
  profile name has been seeded once (`tuning.seed()` is write-once-per-name
  — confirmed by reading the code, not assumed).

Live-tested end to end three times today via real `score run --live-vision`
batches: 0-2 transient failures per batch (self-healing — see below), real
score spread every time, appropriately strict gating (multiple 8-20 photo
batches scored 0 queued when the photos genuinely weren't good enough).

### The 413/rationale bug from the prior session is confirmed fixed
Zero `VisionParseError`-from-rationale-length failures across all of today's
live batches, including ones where 100% of photos escalated to Pro. The
fix (`VisionVerdict.rationale` max_length 140→500, PR #18) holds under real
load.

### Two harmless, self-healing failure modes seen today, not worth chasing
- A `429` upstream rate-limit from OpenRouter, caught by the adapter's
  generic parse-error handler (mislabeled as a schema failure, but harmless).
- A `None` response content field (`TypeError: the JSON object must be str,
  bytes or bytearray, not NoneType`) — a third, rarer response shape not
  explicitly handled.
Both leave the photo un-scored (not in `proj_scores`), so it's automatically
retried in the next batch. Low enough frequency (2-3 out of ~100 calls
today) that it's not worth building retry/backoff logic for yet — revisit if
the rate climbs.

### Vision model research: Qwen3-VL doesn't replace Gemini, don't revisit without new data
Did real market research (live OpenRouter model catalog, not just web
search — a naive AI-summarized fetch of the model list actually **invented
plausible-fake model names** once this session; querying the raw API
directly is the reliable method). Findings, corrected from the prior
session's doc:
- **Qwen3-VL now genuinely supports OpenRouter strict-mode JSON** — reverses
  the prior session's finding that it didn't. Whichever was true when, the
  catalog is the source of truth going forward.
- **DeepSeek is still a dead end** — every DeepSeek model, including the
  newest v4 line, is text-only. No vision input at all. Checked directly.
- **Qwen3-VL is NOT judgment-equivalent to Gemini**, even after the same
  score-anchoring fix applied to Gemini's prompt: it scored every one of 5
  test photos higher than Gemini Pro, +7 to +33 points, averaging +26. After
  anchoring, still averaged **+14 points too generous**, with a new artifact
  (3 different photos scoring an identical 58 — a "safe default" tell).
  **Do not promote to production without a much larger validation sample.**
- Full writeup, including the side-by-side data: `docs/research/2026-08-05-vision-model-cost-eval.md`.

### Docs written or updated today
- `docs/research/2026-08-05-vision-scoring-bottleneck.md` — the original
  49s/photo investigation (prior session, referenced here since it set up
  today's work).
- `docs/research/2026-08-05-vision-model-cost-eval.md` — heavily revised:
  corrected Qwen strict-mode finding, added the side-by-side test, added the
  prompt-anchoring experiment and its "improved, not resolved" verdict.
- `docs/designs/2026-08-05-m6-meta-adapter.md` — M6 slice 1 design (prior
  session, merged today via PR #18).

### Housekeeping
`.claude/settings.json`, `.claude/hooks/`, `.claude/skills/`, `.mcp.json`
were bleed-over from a different project's local Claude Code setup (operator
confirmed: copied over for personal convenience, never meant to be tracked
here). Untracked and gitignored — files remain on disk locally, just not in
this public repo's history anymore.

---

## Landmines discovered today

- **`tuning.seed()` is write-once per profile name.** Editing
  `config/defaults/tuning_profile.json` after the "default" profile has ever
  been seeded does **nothing** — same class of bug as the `pod`/`ops config
  apply` write-once trap already documented below, just in a different
  subsystem. `get_profile()` reads from `tuningprofile.seeded`/
  `tuningprofile.updated` events, not the JSON file.
- **No logging existed anywhere in the scoring pipeline before today.** Only
  two `logger.warning()` calls (the spend-cap check), and no
  `logging.basicConfig` anywhere in the codebase, so even those were barely
  visible. Fixed: `cli.py`'s `main()` now configures logging;
  `pipeline/scoring.py` logs per-photo timing (total/scorers/rescore
  elapsed, escalated, composite) at INFO level.
- **Gate 1 UI queues incrementally during a live batch**, not atomically at
  batch completion. Checking mid-batch shows a true-at-that-moment but
  incomplete count. Not a frontend bug — traced via direct DB/API
  comparison, confirmed the same on two separate occasions today.
- **The commercial-scoring prompt and the OpenRouter JSON schema can drift
  independently** — that's what caused the original rationale-length bug
  (PR #18) and is a class of bug to watch for: anything constrained in one
  but not the other (a Pydantic `Field`, a prompt instruction) can silently
  diverge from what the model is actually allowed to return.

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
| Vision escalation rationale-cap fix | `main` | merged (PR #18) |
| M6 Meta adapter slice 1 (IG/FB interface, no live posting) | `main` | merged (PR #18) |
| Scoring prompt recalibration + escalation band widen | `main` | merged (PR #19) |

**`main` is pushed** — `e2e090e`, `origin/main` in sync, 596 tests, `ruff`
clean. The repo is public and **MIT licensed**.

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
  and a $107.67 canvas — same subject, both ends of the ladder. **Confirmed
  again today**: brown bear cubs are the strongest performers in the Alaska
  batch's Gate 1 results; eagles and whale-tail/documentary shots are not.
- ⚠ **100 products in Gelato vs 27 active Etsy listings.** Worth understanding —
  if most are unpublished, the bottleneck is publishing, not creating.

## Landmines (pre-existing)

- **Template variant ids ≠ store-product variant ids.** Different namespaces.
- **`placeholder` is not `"ImageFront"`** — Gelato names it after the image file
  used when the template was built (yours is `IMG_2485.jpg`). Per-template
  operator data, not a constant.
- **Config was write-once** until `pod config apply` / `ops config apply` existed.
  Editing a JSON file after seeding did nothing — the documented kill switch, the
  price remedy, and the pre-smoke cost population were all silent no-ops. **Now
  confirmed to also apply to `tuning_profile.json`** — see today's landmines above.
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
  RAW-only folders are currently **invisible to the entire system** — the 935
  unpaired Canon R7 photos from the Alaska trip are not part of the 380
  currently being scored.
- M6 Meta adapter slices 2-5 (OAuth flow, live Graph API posting, Gate 3
  integration) — slice 1 merged, nothing beyond it started.

## Research

- `docs/research/2026-08-05-direction-findings.md` — **read this one first.**
  What the survey implies for *direction*: four decisions for you, and a PRD
  discrepancy register (Pinterest is a §3 non-goal; the evidence says it is the
  live channel). Nothing acted on.
- `docs/research/2026-08-05-external-tools-survey.md` — the underlying survey.
  Culling, Lightroom automation, presets, mockups, print prep, Etsy, social.
  Verdicts are BORROW / REFERENCE / **CLOSED**; the CLOSED list exists so we
  stop re-researching Printful, Etsy SEO tooling, and Lightroom cloud.
- `docs/research/2026-08-05-vision-scoring-bottleneck.md` — the 49s/photo
  investigation that found and fixed the rationale-length bug (prior session).
- `docs/research/2026-08-05-vision-model-cost-eval.md` — vision model
  landscape, the Qwen3-VL side-by-side test, and the prompt-anchoring
  experiment. **Read before touching model choice or the scoring prompt
  again** — most of the obvious ideas here have already been tried and
  measured.

## Designs written but not built

- `docs/designs/2026-08-03-raw-only-ingest.md` — M2a
- `docs/designs/2026-08-03-corrections-proposer.md` — M2b (WB/exposure/levelling)
- `docs/designs/2026-08-03-m8-autonomous-operations-draft.md` — M8 (DRAFT)
- `docs/designs/2026-08-05-m6-meta-adapter.md` — M6 slices 2-5 (slice 1 built)
