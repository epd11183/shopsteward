# ShopSteward Capability Audit — 2026-08-10

Evidence-based, multi-agent audit against the five stated capability pillars.
Every finding is grounded in `file:line` or a command run; nothing assumed.
Method: 4 parallel read-only agents (editing / digital-Etsy / Gelato / autonomy)
+ direct measurements. Full suite last measured green at 680 tests
(pre-audit merge of #27); per-area counts below.

## Scorecard

| Pillar | Verdict |
| :-- | :-- |
| 1. Auto uniform-look editing from a folder | **Mostly built; 3 gaps** (WB, tint, LR-verify) |
| 2. Etsy products from finished-JPGs folder | **Built; never live-verified** |
| 3. Etsy API for digital products | **Built (code); UNVERIFIED live** |
| 4. Gelato for physical products | **Built; 1 real defect (price) + config + never live** |
| 5. Autonomous AI revenue management | **Largely MISSING (drafted, not approved — v2)** |

---

## Pillar 1 — Editing: folder → uniform auto-filter

PRESENT (verified): folder-pointed + RAW-only (`edit.py:58,116`); constant look /
color grade across the set + `--batch-lock` (`edit.py:42-84`, `analyze.py:125`);
exposure w/ highlight protection + `exposure_bias` (`analyze.py:42-59`); local
masked shadow lift (`analyze.py:88`, `xmp.py:105`); lens profile + chromatic
aberration flags (`xmp.py:55-59`); contrast/whites/blacks/clarity/dehaze/texture/
HSL/split-toning/tone-curve/vibrance/saturation (`xmp.py:45-74`); per-photo
adaptive + automatic.

GAPS:
- **[P1] White balance / temperature is never corrected.** `xmp.py:43` hardcodes
  `crs:WhiteBalance="As Shot"`; no `crs:Temperature` ever emitted (grep: 0). The
  temp axis is a permanent stub — `analyze._cast_nudge` returns temp `0`
  unconditionally (`analyze.py:104-122`). Mixed-lighting folders will not look
  uniform. The requirement names "white balance/temperature" explicitly.
- **[P1] Tint is computed but silently dropped.** `analyze.py` computes
  `tint_nudge`, stores it on `CorrectionSettings` (`models.py:52`), even averages
  it in batch-lock — but `xmp.compose` never reads it and never emits `crs:Tint`
  (grep: `crs:Tint`=0, `tint_nudge` in xmp=0). Classic computed-then-dropped;
  the requirement names "tint" explicitly.
- **[P2] ACR shadow-mask XMP unverified in Lightroom.** The `MaskGroupBasedCorrections`
  range-mask (`xmp.py:105-126`) is only structurally tested; the code's own
  docstring (`xmp.py:8-10`) says it MUST be confirmed in Lightroom — no evidence
  that smoke ran. If the schema is subtly wrong, the shadow lift silently no-ops
  on import.

## Pillar 2 / 3 — Digital Etsy products from a JPGs folder

PRESENT (verified): folder intake incl. unmatched manual JPGs (`landing.py:149`,
`shop.py:78`); draft build → images/mockups → copy → pricing (`drafts.py:125`);
**digital sellable-file upload** create→image→file→update (`push.py:238`,
`_file_stage` `push.py:159`); vision-assisted copy (`vision_copy.py`, `copy.py:82`);
`LiveEtsyWriteAdapter` full create/image/file/update/price/publish
(`adapters/etsy/live.py:83-193`) + OAuth PKCE + token refresh (`adapters/etsy/auth.py`);
triple gate (`live_gate.py:80`); Gate 3 publish (`gate3.py`); **186 fixture tests pass**.

GAPS:
- **[P1] Never run against the live Etsy API.** All 186 tests use the Fake
  adapter; the real write path (form-encoding, `taxonomy_id`, `updateListingInventory`
  price whitelist, multipart image/file upload) is unverified. Operator smoke is
  unchecked (`plans/…phase-a.md:356`). No scrubbed live fixture, no
  `gate3.published` evidence in `receipts/`.
- **[P2] Offline default = fixture copy.** `shop build` with no flags yields canned
  copy + no vision signal; real copy needs `--live-vision` + `--live-copy`
  (operational prerequisite, not a code gap).

## Pillar 4 — Gelato physical products

PRESENT (verified): variant selection + aspect routing + DPI drop
(`pod/catalog.py:122`); real pricing/margins/floors, acrylic+poster costs
`costs_verified_on 2026-08-04` (`pod/pricing.py`, `pod.json`); print-file resolve
+ R2 host (`pod/printfile.py`, `adapters/printfile/live.py`); create→poll→link
idempotent (`pod/provider.py:137`); live Gelato adapter with write-safety
`isVisibleInTheOnlineStore=False` pinned (`adapters/pod/live.py:72`); enrich
(`pod/enrich.py`); one-command orchestration (`shop.py:49`); **125 tests pass**.

GAPS:
- **[P0] `retail_price` is never sent to Gelato, and enrich never sets price →
  price is set NOWHERE on the live path.** Verified: `adapters/pod/live.py:66-95`
  create body has no price field (only a `ponytail:` comment at :85); `enrich.py:8`
  deliberately doesn't set price. A physical listing could go live below the
  margin floor or at a wrong price. Must confirm Gelato's real price mechanism
  (a variant field or a separate call) and wire + test it.
- **[Config] 15 unfilled placeholders in `pod.json`** — canvas/canvas_portrait
  `template_id`/`variant_key`/`placeholder` = `<OPERATOR>`, plus `gelato.store_id`
  = `REPLACE_AT_C3…`. Acrylic + poster are live-ready; canvas types are not.
- **[P2] `store_id` placeholder not rejected by validation** — model rejects
  `<OPERATOR>` but not `REPLACE_AT_…` (`models.py:49-58`), so a live run would
  POST to a bogus store URL and fail at HTTP instead of up front.
- **[Verify] No live Gelato/R2 smoke has run** — 125 tests all fake/respx.

## Pillar 5 — Autonomous AI revenue management

**No autonomous entrypoint exists.** The app is entirely operator-invoked CLI +
human Gate 1/2/3. The capability maps to milestone M8a, which is a DRAFT design
explicitly marked "NOT APPROVED, NOT A PROPOSAL TO BUILD YET"
(`docs/designs/2026-08-03-m8-autonomous-operations-draft.md:1-4`); only its
read-only analytics brief (slice 1) is built.

PARTIAL: analytics ingestion (gated; **active-listings-only ceiling** —
`adapters/etsy/live.py:66`); event-sourced substrate + `proj_listing_daily`
(`pipeline/ops/`); Etsy write primitives exist; human-gated guardrails.

MISSING (verified by grep + in-code "slices 2+" docstrings):
- No scheduler / daemon / agent loop / autonomous entrypoint (grep: none).
- No feedback loop analytics → tuning (`tuning.py` is seed+read only).
- No revenue-optimizing actions: reprice-live, re-tag/SEO, renew/promote,
  pause-underperformer, gap-fill product creation, A/B copy (grep: none in
  non-test code). Publish's only caller is the Gate-3 human endpoint.
- No goal/policy object ("maximize revenue" as a driven objective — grep: none).
- No autonomy guardrails (spend caps, kill-switch, tiers, rate limits, undo).
- No `ops` HTTP surface / no `run|approve|halt|status` verbs anywhere.
- No action/audit projection.
- Meta/Instagram adapter exists but is imported by nothing (dead code).
- Note: PRD §3.2 lists autonomy as a v1 **non-goal**; several sub-capabilities
  may be Etsy/Meta-policy-constrained (relist, coupons, ads, buyer messaging) —
  must be verified against live policy, not assumed.

---

## Remediation plan (prioritized)

### P0 — correctness (would ship wrong)
- **R1. Gelato price wiring.** Confirm how Gelato accepts per-variant retail price
  on create-from-template (field vs separate pricing call — verify against live
  API/docs, not assumption); wire `retail_price` into `create_product`; assert it
  in `test_live_gelato.py`. Until then physical listings must not go live.
  *(Pillar 4; `adapters/pod/live.py`)*

### P1 — blocks a stated capability
- **R2. White-balance / temperature correction.** Either compute a real
  temperature estimate (per-camera calibration table, or a bounded neutral-target
  method) and emit `crs:Temperature`, or make an explicit product decision to
  ship as-shot-only and drop the requirement. *(Pillar 1; `analyze.py`, `xmp.py`)*
- **R3. Tint correction wiring.** Emit `crs:Tint` from `correction.tint_nudge` in
  `xmp.compose` (with the existing cap), closing the computed-then-dropped path;
  add a test asserting a green-cast frame writes a magenta tint. *(Pillar 1)*
- **R4. Live Etsy digital smoke.** Push one real draft end-to-end on a test shop;
  verify taxonomy/encoding/inventory-price/upload; record a scrubbed fixture;
  check the operator boxes. *(Pillar 2/3; operator + `adapters/etsy/live.py`)*
- **R5. Live Gelato + R2 smoke (acrylic/poster, after R1).** One winner →
  `--live-gelato --live-printfile`; confirm Etsy DRAFT + correct price; delete to
  clean up. *(Pillar 4; operator)*
- **R6. Reject the `REPLACE_AT_…` store_id placeholder** in `PodProviderRef`
  validation (fail fast, like `<OPERATOR>`). *(Pillar 4; `adapters/pod/models.py`)*

### P2 — verification / quality
- **R7. Lightroom ACR shadow-mask smoke.** Open one generated `.xmp`; if LR
  rejects/ignores the mask, fix `_shadow_mask` schema. *(Pillar 1)*
- **R8. Fill operator Gelato config** — canvas/canvas_portrait IDs + `store_id`.
  *(Pillar 4; operator)*
- **R9. Fix active-only listing visibility** — read inactive/expired/draft states
  in the Etsy read adapter; prerequisite for any lifecycle action. *(Pillar 5 foundation)*

### P3 — autonomy program (v2; needs approval + policy verification first)
- **R10. Approve + scope M8a**; verify Etsy/Meta policy on relist, coupons, ads,
  buyer messaging (resolve draft §0 questions P1–P10) — no assumptions.
- **R11. Autonomy chassis** — capability registry + governor (spend caps,
  kill-switch, tiers, rate limits, undo) + runner/scheduler + proposal/action
  event stream + `ops` API (`run|approve|halt|status`).
- **R12. Goal/policy object** — start narrow (revenue per active listing over a
  window) → ranked candidate actions.
- **R13. Feedback loop** — wire `analytics.py` → `tuning.py` writes (the PRD's
  promised, currently-no-op loop).
- **R14. Revenue actions as governed ops** terminating in Gate-3 drafts: reprice,
  SEO re-tag, renew, pause-underperformer, gap-fill, A/B copy.
- **R15. Promotion loop** — wire the Meta/IG adapter (blocked on policy Qs).

## Bottom line
Pillars 1–4 are **built and fixture-green**; the real blockers are (a) one POD
pricing defect (R1), (b) two editing color-correction gaps the requirement names
(R2/R3), and (c) **nothing has been verified against a live API** (R4/R5/R7).
Pillar 5 (autonomy) is essentially unstarted and is a v2 program gated on
approval + external-policy verification.
