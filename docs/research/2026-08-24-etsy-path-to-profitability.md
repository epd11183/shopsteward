<!-- /autoplan restore point: ~/.gstack/projects/epd11183-shopsteward/main-autoplan-restore-20260825-092051.md -->
# PhotosByEricD (shop 52644245) — Path to Profitability

**Author:** Claude, acting as shop manager under the M8a/M8b autonomy chassis, per operator direction 2026-08-24 ("this is your store, decide what you want to do... create your business plan"). Operator-funded floor: $20/month through the autonomy budget; beyond that, the shop must self-fund.

## 1. Where the shop actually stands (ground truth, 2026-08-24)

Pulled from `ops brief` and Etsy live sync, not estimated:

- **Revenue, last 7 days: $0.00.** 0 orders, 0 units.
- **Lifetime sales across all 27 listings: effectively 2** (the two listings `listing.renew` and `listing.seo_edit` are willing to touch on the strength of "≥1 lifetime sale" — everything else has zero).
- **Traffic is thin, full stop.** The single most-viewed listing has 87 lifetime views, ever. 16 other listings have 1–20 lifetime views apiece. This is not a filtered/recent number — it's all-time.
- **Product mix:** 5 canvas, 19 digital download, 3 unrecognized-from-title. No mugs, puzzles, pillows, or other POD formats live yet, despite an earlier proposal to diversify — that work was blocked on photo-archive linkage until today.
- **Autonomy spend to date: $0.40 of the $20/month cap** (two $0.20 renewals). Governed capabilities (`renew`, `seo_edit`, `reprice`) are live and already executing on real listings under operator-approved policy.
- **Archive linkage:** as of today, 8 of 27 listings have a confirmed local source photo on file (via the new `archive adopt-local` pHash matcher), unlocking `listing.gapfill_reprint` *in principle* for those 8 — see §3 for why "in principle" is doing real work in that sentence.

## 2. The actual bottleneck is traffic, not conversion or catalog breadth

It would be easy to read "16 listings viewed but never sold" as a conversion problem — bad titles, bad photos, wrong price — and go fix those. I don't think that's honest with these numbers. A listing with 5–20 *lifetime* views doesn't have a conversion problem yet; it doesn't have a large enough sample to have any problem *diagnosed*. Etsy's own search algorithm won't meaningfully favor a listing until it has enough of a track record to rank on — and almost nothing here has that.

Two of the automation capabilities built this session make this concrete instead of a hunch: `listing.gapfill_reprint` (reprint a proven photo into a new product format) and `social.caption_draft` (draft a promo caption) are **both deliberately gated on `analytics.top_sellers()` — a real sale in the last 7 days.** That's not a bug; it's the honest design choice made earlier this session ("never guess a reprint/caption for a listing with no evidence it sells"). But it means the most powerful parts of the chassis are currently inert, because there's no sale yet to bootstrap from. The shop is in a chicken-and-egg state: the tools that compound success need one success to compound.

SEO/price polish (`seo_edit`, `reprice`) helps at the margin and costs nothing extra, so it should keep running — but it optimizes the *last* mile (a visitor who's already looking at the listing), and right now almost nobody is getting that far. Fixing the top of the funnel is the actual unlock.

## 3. What I already changed this session

- **Lowered `seo_edit`/`reprice`'s `min_lifetime_views` threshold from 25 → 5** (`config/defaults/ops.json`). At this shop's real traffic scale, 25 lifetime views was a bar almost nothing could ever clear — it was silently excluding every one of the 16 "viewed but never sold" listings from ever being flagged for a refresh. 5 is still a deliberate floor (not 1) to avoid churning copy on listings with too little signal to judge, but it now actually catches real listings (20, 18, 8, 5 lifetime views).
- **Ran the archive matcher against `D:\Photos\Etsy\sample\full\`** (see prior turn) and adopted 8 confirmed photo↔listing links — real work, but its payoff (`gapfill_reprint`) is gated behind §2's chicken-and-egg problem until a sale happens.
- **Approved the one pending renewal** in the queue; it's currently held by `weekly_catalog_pct_cap` (the shop's own weekly action-rate limiter, already maxed by this week's other actions) and will retry automatically when the window resets — expected governor behavior, not a fault.

## 4. Traffic research (web, 2026-08-24)

Two searches, sources below. Findings that actually change what I'd prioritize:

- **Pinterest drives ~33% of Etsy's organic external traffic — more than Google, Instagram, and Facebook combined** — and it's a visual *search* engine (users have buying intent), which fits wall-art/print listings unusually well: pins surface for months to years after posting, unlike a social feed post that dies in a day. [Craftybase](https://craftybase.com/blog/pinterest-for-etsy-seller-handmade), [Printify](https://printify.com/blog/how-to-use-pinterest-for-etsy/)
- Realistic timeline: **3–6 months before Pinterest's organic traffic becomes significant**, but it compounds afterward in a way paid ads don't. This is a "start now, judge in Q4" channel, not a this-week lever.
- General Etsy guidance for zero-review shops: **Etsy's 2026 algorithm treats "review velocity" as a real ranking signal** — a shop with 0 sales/0 reviews is treated as higher-risk and gets less organic push, which is consistent with what the numbers above already show. [Slayva](https://slayva.com/etsy-shop-no-sales-2026-fix/), [Gelato](https://www.gelato.com/blog/how-to-get-more-traffic-on-etsy)
- Refreshing a listing's title/tags meaningfully (not cosmetically) is reported to produce a short-term "fresh content" impression bump — consistent with what `seo_edit` already does, reinforcing that it's worth keeping on, just not sufficient alone.

Sources: [Gelato — How To Get More Traffic On Etsy](https://www.gelato.com/blog/how-to-get-more-traffic-on-etsy) · [growingyourcraft.com — Why Your Etsy Shop Is Not Making Sales](https://www.growingyourcraft.com/blog/reasons-why-your-etsy-shop-is-not-making-sales-how-to-fix-them) · [Slayva — Etsy Shop No Sales 2026](https://slayva.com/etsy-shop-no-sales-2026-fix/) · [Craftybase — Pinterest for Etsy Sellers](https://craftybase.com/blog/pinterest-for-etsy-seller-handmade) · [Printify — How to use Pinterest for Etsy](https://printify.com/blog/how-to-use-pinterest-for-etsy/)

## 5. The plan, phased by trigger (not calendar date)

**Phase 1 — now, funded entirely by the existing $20/month, no new spend:**
1. Keep `renew`/`seo_edit`/`reprice` running under governed autonomy exactly as-is (already live, already cheap, already caught by the operator's own caps).
2. **Pinterest, starting manually.** No adapter exists in this codebase yet (`adapters/meta` covers Instagram/Facebook only, and isn't wired to a live implementation either). Building a live Pinterest adapter is a *new external service integration* — under CLAUDE.md that needs your explicit sign-off before I build it, so I'm not doing that unilaterally. What I *can* do without waiting: use `social.caption_draft`'s sibling pattern to hand you ready-to-pin descriptions/boards for the 5 canvas + best digital listings, so posting is copy-paste, zero engineering required, and can start today if you want it. Say the word and I'll draft the first batch.
3. No paid Etsy Ads spend recommendation right now — the $20/month floor is allocated to the governed capabilities above; diverting any of it to ad spend is a real budget decision, not mine to make unilaterally, and I don't think it's the right first move anyway before there's a review/sales base for the algorithm to reward it (see §4).

**Phase 2 — triggers automatically on the shop's first real sale in a 7-day window (no calendar date; could be next week or next quarter):**
- `listing.gapfill_reprint` activates for whichever of the 8 now-archived listings sold, proposing new POD formats (mug/puzzle/pillow/etc.) from the *same already-proven photo* — zero new photography, zero new manual work, funded by the same $20 floor since it only produces an unpublished draft (Gate 3 stays your manual approval).
- `social.caption_draft` activates for the same listing, giving you ready promo copy for whatever channel (Pinterest, IG, wherever) is working.
- I'll expand archive-matching coverage to more of the remaining 19 unlinked listings as photo access allows, so more of the catalog is reprint-eligible by the time it matters.

**Phase 3 — conditional, self-funded only, not the $20 floor:** the AI-generated novel (non-photo) product idea you raised. I'm deliberately **not** prioritizing this now, and want to be direct about why: there is zero evidence yet that this niche's buyers want anything beyond photography-based wall art, generating art costs real OpenRouter tokens plus a Gelato/Printful base cost per test SKU, and the shop hasn't yet proven it can sell what it already has. Spending the fixed $20 on a new speculative product line before Phase 1/2 prove out would be optimizing the wrong stage of the funnel. If Phase 1/2 produce real revenue, I'd revisit this as a small (1–2 SKU) test funded by that revenue — and per CLAUDE.md, using a new AI-generation flow for real published designs is worth a deliberate go/no-go conversation with you at that point, not something I'd quietly turn on. The hard guardrail stays intact regardless: AI never touches or regenerates an existing photograph — this would only ever be wholly new, separately-generated artwork.

## 6. What I need from you

- **Nothing blocking today.** Everything in Phase 1 that needs your sign-off (Pinterest adapter build, ad spend, Phase 3) is flagged above rather than started.
- If you want the Pinterest copy-paste drafts, say so and I'll generate the first batch this session.
- Otherwise: I'll keep running the governed autonomy loop, keep expanding archive coverage as photos allow, and revisit this plan once real sales data exists to replace the "0 sales" baseline above.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

/autoplan review 2026-08-25 (branch main, ccf660b). Full CEO plan: `~/.gstack/projects/epd11183-shopsteward/ceo-plans/2026-08-25-autonomous-etsy-shop.md`. Voices: Codex CLI + independent Claude subagent, both phases.

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | CEO | Mode = SELECTIVE EXPANSION | Mechanical | autoplan override | forced by /autoplan | — |
| 2 | CEO | Premise gate → C: Pinterest + owned-channel IG/FB push | OPERATOR (D1) | — | 75% of historical sales from owned network; plan omitted it | A (Pinterest-only), B (owned-only) |
| 3 | CEO | Accept scope items 2-12 (baseline fix, dedup, cap-revert tracking, kill criteria, holdout, unit economics, positioning, seo cooldown, staleness alarm, register reconciliation, dashboard data) | Mechanical | P1/P2 | in blast radius, <1d each, both voices support | — |
| 4 | CEO | Landscape check = plan's own 08-24 research + voices; no re-search | Mechanical | P3 | one day old; both voices flagged the stat as weak anyway | fresh WebSearch |
| 5 | CEO | Skip inner spec-review loop on CEO plan doc | Mechanical | P3 | two independent voices already reviewed the same content | 3-iteration subagent loop |
| 6 | Eng | Dedup at runner level + action.expired sweep + action.superseded | Mechanical | P5 | root cause is proposal lifecycle, not _candidates() | stabilizing action_id (load-bearing for idempotency) |
| 7 | Eng | Holdout enforced in govern() with documented priority | Mechanical | P5 | per-capability checks make registration order silent business logic | _candidates()-level checks alone |
| 8 | Eng | Staleness escalation = read-time brief computation, no events | Cross-model tension | P5 | replay determinism; events are facts, reminders are derived | Codex's bounded reminder events |
| 9 | Eng | Owned-channel = per-channel eligibility policy config + caption mark-posted + channel in target identity | Mechanical | P4/P5 | avoids doc contradiction and target_id collision | copy-paste explore policy into caption_draft |
| 10 | Eng | P2 primary outcome = Pinterest outbound clicks; Etsy view deltas secondary | Mechanical | P1 | correlational deltas confounded even with holdout | views-only readout |
| UC1 | Gate | Etsy Ads diagnostic → DEFERRED, revisit after 30d of pin data | OPERATOR | — | operator call at final gate 2026-08-25 | approve now / reject outright |
| UC2 | Gate | Catalog expansion APPROVED: new capability proposing paced digital listings from archive (Gate-3 approval per listing, existing $20 funds fees) | OPERATOR | — | both voices: catalog is the missed 10x; archive is the unfair asset | keep sale-gated |
| UC3 | Gate | Phase-2 trigger WIDENED: trailing-90d OR lifetime sale per listing + views-velocity alternative trigger; proposals stay PROPOSE-tier | OPERATOR | — | 7-day window statistically indefensible at this sales rate | 7-day gate, lifetime-only |
| TD1 | Gate | P2 live Pinterest read adapter AFTER first ~30d pin readout; chassis P1 fixes first | OPERATOR | — | read infra premature before manual pins produce data | build P2 now |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open (via /autoplan) | 16 proposals, 12 accepted, 2 deferred |
| Codex Review | `/codex review` | Independent 2nd opinion | 2 | issues_found (via /autoplan voices) | CEO: 24 concerns; Eng: ~20 concerns |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_open (via /autoplan) | 12 issues, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | SKIPPED | no UI scope |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | SKIPPED | no developer-facing scope |

- **CROSS-MODEL:** Exceptional overlap — both models independently challenged the traffic-bottleneck premise, the Pinterest 33% stat, the 7-day Phase-2 trigger, missing kill criteria, and the confounded P1 readout. One tension (staleness events vs read-time) resolved for read-time.
- **VERDICT:** CEO + ENG CLEARED — all findings folded into 24 tasks; all 4 operator decisions resolved at the final gate 2026-08-25 (UC1 defer-ads, UC2 approve-catalog-expansion, UC3 widen-trigger, TD1 P2-after-readout). Ready to implement.

NO UNRESOLVED DECISIONS
