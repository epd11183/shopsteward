# M8 Design — Autonomous Shop Operations (Stage 5 + business path)

*Status: **DRAFT — NOT APPROVED, NOT A PROPOSAL TO BUILD YET.** Architect output
2026-08-03, produced at the operator's request late at night with the
conversation explicitly deferred. Nothing here is decided. The purpose of this
document is to make the decisions **legible** and the tradeoffs **sharp** so the
deferred conversation is short. §17 is the point of the document; §3 is the
argument. Decision candidates are numbered from 70 (42–49 belong to M5b, 50–56 to
M2b). Read §0 before anything else — several capabilities in the requested scope
may not be legal or may not exist as APIs, and I have not verified either.*

**Operator's framing, verbatim, which this design must serve:** *"I'm the artist
supplying the picture and making final edits on photos, but I want AI to be the
shop owner, business manager, marketer, etc."*

---

## 0. Reality check — READ FIRST

Three classes of fact are mixed together in the requested scope, and treating
them alike is how this milestone goes wrong.

**(a) Verified in this repo, today.** `src/shopsteward/core/sync.py` already
appends `etsy.shop.observed`, `etsy.listing.observed` and `etsy.sale.observed`
on every sync, and it appends **one `etsy.listing.observed` per listing per
run, unconditionally** (`sync.py:29`). The event log therefore *already contains
a per-listing time series of views, favourites, price and state* — every sync
ever run is still in there. `core/projections.py:47` throws it away
(`INSERT OR REPLACE INTO proj_listings`, last-write-wins). **A performance time
series costs one new projection and zero API calls.** That is the single
cheapest, highest-value thing in this entire document.

Also verified: `auth.py DEFAULT_SCOPES = ("listings_r", "listings_w",
"transactions_r", "shops_r")`. `listings_w` is already consented (M5a). Receipts
already parse into `EtsyReceipt.transactions[]` carrying `listing_id`, so
per-listing sales attribution exists. `LiveEtsyAdapter.list_listings()` hits
`/shops/{id}/listings/active` — **active only**; expired, sold-out and draft
listings are invisible to us today. Retiring dead listings needs that fixed.

**(b) NOT verified, and I refuse to assume. Every one of these is a
CLAUDE.md "load-bearing question — stop and ask".** Each needs an answer read
off Etsy's or Meta's own current policy/reference, not inferred:

| # | Question to answer from the source | What collapses if the answer is "no" |
|---|---|---|
| P1 | Does Etsy Open API v3 expose **any** endpoint to read or send buyer messages/conversations? My belief is **no** — but belief is not verification. | The whole "customer communication" block becomes unbuildable, not merely unwise. |
| P2 | Does Etsy's **API Terms of Use** permit machine-composed messages sent to buyers under a seller's token? | Buyer messaging is human-only by law of the platform, not by our choice. |
| P3 | Does Etsy's Seller Policy treat **programmatic renew/relist** as search manipulation? Renewal churn to reset "recently listed" is a known suspension surface. | Auto-renew *management* survives; auto-**re**listing dies. |
| P4 | What does Etsy prohibit in **review solicitation**? | Any "ask for a review" capability dies. |
| P5 | Is there a public **Etsy Ads API** at all? My belief is **no**. | Paid Etsy acquisition is not automatable at any tier; it's a dashboard job. |
| P6 | Do **sales/coupons** have v3 write endpoints, and under which scope? | Promotions become manual. |
| P7 | Current Etsy **rate limits** (req/sec and req/day) and whether bulk listing edits are separately throttled. | Sets the ceiling on any batch capability. |
| P8 | Which **scopes** each write above needs (`listings_w` vs `transactions_w` vs `shops_w` vs `billing_r`), and whether own-shop personal access still suffices (decision 35) or this crosses into commercial access. | Each new scope = another `shopsteward etsy auth` re-consent, a §8.2 secrets decision. |
| P9 | Meta Graph: IG Business/Creator + linked FB Page in **one** integration (operator's own note — sounds right, unverified); content-publishing limits (believed 25 posts/24h); whether posting to a **Page** needs additional review beyond IG. | Sizes the Facebook slice. |
| P10 | Meta **Marketing API**: app review + business verification requirements. | Meta ads are a project, not a slice. |

**(c) Structurally true regardless of the answers.** PRD §3.2 lists as v1
**non-goals**: *"Order management, customer service, or shipping — Etsy and the
POD providers own these"* and *"Channels beyond Etsy and Instagram (Pinterest,
Facebook, TikTok deferred to v2)"*. Half of M8's requested scope is explicitly
out of PRD v1. **M8 is therefore a v2 milestone, or PRD §3.2 must be amended.**
Flagging per CLAUDE.md ("when PRD and this file disagree, the PRD wins; flag the
discrepancy"). This is operator decision §17.3 and it is free to make — but it
must be made consciously, not by writing code that quietly contradicts the PRD.

Also structurally true: PRD v2 §5.5 (which v2.1 carries forward unchanged)
**already specifies Stage 5** — nightly pull into time-series performance facts,
weekly bucketing into `performing / refresh-mockup / refresh-title /
refresh-tags / archive / repurpose`, and — critically — *"refresh actions are
prepared automatically and wait at Gate 3"*. The PRD's own answer to "how
autonomous is listing management" is **prepare automatically, approve with one
tap**. The model below must be consistent with that, and is.

---

## 1. The central problem: two axes, not one

The three gates are a **per-hero, synchronous, creative** control: curate,
finish, publish. They exist to bound *creative* attention to three moments per
photograph. A fourth creative touchpoint is forbidden.

M8 is a **per-shop, continuous, commercial** control. Its actions are not about
one photograph; they happen on a schedule, forever, after publication, and they
are customer-visible, money-moving and frequently irreversible. Applying the
gate rule here is a category error in both directions:

- **"Zero touchpoints, it's all unattended"** is how the AI messages a grieving
  customer at 3am, or deactivates 40% of the catalog because a sync returned
  zeros.
- **"Approve everything"** is not a fourth *gate* — it is an unbounded stream of
  interruptions, which is worse than a gate and is exactly what the operator is
  trying to escape.

So M8 needs its own control model, orthogonal to the gates. It must be
**derivable** (a new capability classifies itself; we don't re-litigate each
one), **earned** (nothing starts autonomous), and **bounded** (caps, kill switch,
undo).

One consequence worth stating up front, because it removes work: **any capability
whose output is an unpublished Etsy draft is already governed by Gate 3.** It
needs no autonomy tier at all. "Restock this photo as a canvas", "propose a
bundle", "prepare a title refresh" all end in a draft, and Gate 3 already
publishes drafts with one tap. That folds a large slice of the requested scope
back into the existing gate structure at zero new risk.

---

## 2. Proposed autonomy model — four tiers, one derivation rule, one ladder

### 2.1 The tiers

| Tier | Name | Behaviour | Operator sees |
|---|---|---|---|
| **T0** | **Auto** | Do it. Log it. | A line in the daily brief. |
| **T1** | **Auto + Notify** | Do it. Log it. Surface it prominently with a one-click **Undo**. | A card at the top of the brief for 7 days. |
| **T2** | **Propose** | Prepare it fully, execute nothing. One tap approves; one tap rejects. Expires after `proposal_ttl_days`. | An approval queue in the brief. |
| **T3** | **Operator-only** | Never automated. The system may *assemble context* (draft text, a summary, the relevant data) but a human performs the act. | Nothing, or an assist artefact. |

### 2.2 The derivation rule — the important part

A capability's tier is **not chosen**. It is the **maximum** of three axis
scores. This is what makes the model survive contact with capabilities nobody
has thought of yet.

| Score | **Reversibility** | **Audience** | **Money** |
|---|---|---|---|
| **0** | Undo is a local DB/config change; nothing outside changed | Nobody outside sees it | $0 |
| **1** | Undo is one API call we already implement, and the world reverts | Shop visitors see it in aggregate (a listing, a public post) | $0, but it changes revenue mechanics (price, availability) |
| **2** | Undo requires a third party, a public correction, or time | A **named buyer** receives it | Spends from a capped budget |
| **3** | Cannot be undone: a message read, money moved, a review posted | A named buyer receives it in a **complaint/dispute** context | Spends outside a cap, or issues a refund |

`tier = max(reversibility, audience, money)`.

Two corollaries fall straight out and settle most arguments:

- **A budget increase can never be T0 or T1.** Money ≥ 2 by construction.
- **Anything a named human reads can never be T0 or T1.** Audience ≥ 2 by
  construction.

### 2.3 Capability contract (enforced at registration, not by review)

```python
class Capability(Protocol):
    key: str                     # "listing.autorenew_off"
    max_tier: Tier               # CODE-LEVEL CEILING. The ladder may never exceed it.
    def propose(self, ctx) -> list[ProposedAction]: ...   # pure-ish; reads only
    def execute(self, action) -> ExecutionResult: ...     # returns before/after
    def undo(self, action) -> None: ...                   # REQUIRED above T2
    def estimate_cost_usd(self, action) -> float: ...
```

Invariants the registry enforces, in code, at import time:

1. **A capability with no working `undo()` cannot be registered above T2.** Ever.
2. **`max_tier` is written in Python next to the capability, not in config.**
   Config can only move a capability *down*. An operator can be more cautious
   through config; nobody can be less cautious without a code change and a
   review.
3. **Every capability's first live execution is T2**, regardless of `max_tier`.
   Nothing ships autonomous.
4. Every `ProposedAction` carries a human-readable `reason` and an `inputs_hash`
   of the exact data it read. An action whose reason cannot be rendered in one
   sentence does not execute.

### 2.4 The promotion ladder — autonomy is earned, and lost instantly

Capabilities move within `[T2, max_tier]` only:

- **T2 → T1:** ≥ `promote_approvals` (default 20) operator approvals, **zero**
  rejections, **and** ≥ `promote_min_days` (default 14) elapsed. Both the count
  and the clock are required — otherwise a batch of 20 approvals in one evening
  promotes on inattention.
- **T1 → T0:** ≥ 30 notified executions, **zero** undos, ≥ 30 days.
- **Demotion is immediate and asymmetric:** one operator rejection, or one
  operator undo, drops the capability one tier and **resets both counters to
  zero**. No appeal, no averaging.

All thresholds live in `config/defaults/ops.json`. The ladder state is a
projection, rebuilt from events.

This is the mechanism that makes the whole thing safe *and* cheap: it is a
counter and a date comparison. It means the operator does not have to guess
which capabilities deserve autonomy — the system finds out, on real actions,
and takes back autonomy it has abused.

### 2.5 Why not just "a fourth gate"

Rejected explicitly — see §15. Gates are per-hero and synchronous with creation.
A shop-operations action has no hero and no creation moment; routing it through
Gate 3 means an ops action cannot happen unless a photograph happens to be
publishing that day. The Brief (§6) is the ops surface; Gate 3 stays the
creative one.

---

## 3. THE PLACEMENT TABLE

`R/A/M` = the §2.2 axis scores. **Tier = max(R,A,M)** in every row; where
something stricter than the formula is proposed, the reason is stated.
`max_tier` is the ceiling; capabilities start at T2 and climb toward it (§2.4).

### 3.1 Listing lifecycle (Stage 5)

| # | Capability | R | A | M | Tier / ceiling | Defence |
|---|---|---|---|---|---|---|
| 1 | Etsy sync (listings, receipts, shop) | 0 | 0 | 0 | **T0** | Read-only. Already exists (M1). |
| 2 | Build performance time series (`proj_listing_daily`) | 0 | 0 | 0 | **T0** | Pure projection over events we already hold. Zero API calls. |
| 3 | Generate the daily/weekly brief | 0 | 0 | 0 | **T0** | Writing a report changes nothing. |
| 4 | Bucket listings (performing / stale / dead) — **report only** | 0 | 0 | 0 | **T0** | PRD §5.5's weekly scoring run, minus the acting. |
| 5 | **Turn OFF auto-renew** on a dead listing | 1 | 1 | 1 | **T1** | One field, one call back on. Nothing visible until expiry (up to 4 months of undo runway). Saves $0.20/listing/cycle. *This is the ideal first write.* |
| 6 | Turn auto-renew back ON (a listing revived) | 1 | 1 | 1 | **T1** | Symmetric with 5. Costs $0.20 later; that's inside the fee model, not a budget. |
| 7 | **Deactivate** a dead listing (state → inactive) | 1 | 1 | 1 | **T1**, ships T2 | Immediately customer-visible (the shop looks smaller) and interacts with §0 P3 policy. Reversible in one call. Earn it on the ladder. |
| 8 | **Renew/relist** a listing (pay to reset) | 2 | 1 | 2 | **T2** ceiling | Spends, and is the *exact* pattern §0 P3 asks about. **Blocked pending P3.** |
| 9 | **Price change** on a live listing | 1 | 1 | 2 | **T2** ceiling, never higher | A buyer who saw $12 yesterday sees $18 today. Revenue mechanics = Money 2 by definition. |
| 9b | Price change on a **POD** listing | — | — | — | **T3 / forbidden** | Decision 43 already forbids it: touching `updateListingInventory` rewrites provider SKUs. Change `pod.json` markup and rebuild. |
| 10 | Title / tag / description edit on a **live** listing (SEO iteration) | 1 | 1 | 1 | **T2** ceiling | Formula says T1. **Overridden to T2** on two grounds: §0 P3 policy surface, and edits reset listing history so a bad iteration is not cleanly reversible in Etsy's ranking even if reversible in our API. PRD §5.5 independently says the same ("wait at Gate 3"). |
| 11 | Swap mockup images on a live listing | 1 | 1 | 0 | **T2**, ceiling T1 after M7 | Same class as 10, less policy exposure. Genuinely promotable once M7 can show a mockup style wins. |
| 12 | **Inventory management** | — | — | — | **CUT** | Digital quantity is 999 and does not decrement; POD is made-to-order. **There is no inventory problem to solve.** Removing it from scope is the whole design. |
| 13 | Receipts → order/bookkeeping export (CSV) | 0 | 0 | 0 | **T0** | Reading and writing a local file. |
| 14 | Detect a **stuck/failed POD order** and raise it | 0 | 0 | 0 | **T1** (notify) | Read + tell. *Not in the operator's candidate list and arguably the highest-value item in it* — a silently failed Gelato order means a buyer paid and got nothing. Needs a provider order API = new integration (§17.14). |
| 15 | **Act** on a failed order (reorder, refund, apologise) | 3 | 3 | 3 | **T3** | Every axis maxes out. |
| 16 | Build a **draft** listing (new format, restock, bundle) | 0 | 0 | 0 | **T0** — *and needs no tier* | Ends in an unpublished draft; **Gate 3 is already the approval.** Reuses M5a/M5b wholesale. |

### 3.2 Customer communication

| # | Capability | R | A | M | Tier | Defence |
|---|---|---|---|---|---|---|
| 17 | Reply to a buyer message | 3 | 3 | 0 | **T3** | A read message cannot be unread. §13.2's asymmetry argument. **Also possibly unbuildable — §0 P1/P2.** |
| 18 | *Draft* a reply for the operator to send | 0 | 0 | 0 | **T3 (assist)** | Formula says T0, because nothing is sent. **Overridden**: a fluent draft addressed to a real customer invites paste-without-reading, which converts a T0 into a T3 through human factors. If it ships at all it ships as a *bulleted context summary*, not a ready-to-send paragraph. Out of v1. |
| 19 | Respond to a public review | 3 | 3 | 0 | **T3** | Public and permanent. |
| 20 | Issue a refund | 3 | 3 | 3 | **T3** | Money out, cannot be un-refunded. |
| 21 | Handle a dispute / case | 3 | 3 | 3 | **T3** | — |
| 22 | Answer a shipping question | 3 | 3 | 0 | **T3** | The tracking data isn't even in our system (M5b §11 excluded orders). |
| 23 | Solicit a review | 3 | 3 | 0 | **T3 / likely prohibited** | §0 P4. Do not build until answered. |

**The whole block is T3. That is the recommendation, and it is not timidity —
it is §13.2's arithmetic.**

### 3.3 Marketing

| # | Capability | R | A | M | Tier | Defence |
|---|---|---|---|---|---|---|
| 24 | Post a **Gate-3-approved** IG asset on schedule (M6) | 1 | 1 | 0 | **T0** | The approval already happened at Gate 3. Posting is execution of an approved decision. |
| 25 | Cross-post that same approved asset to a **Facebook Page** | 1 | 1 | 0 | **T1** | Same asset, same approval, one more surface. Cheap **if and only if** M6 builds `adapters/meta` rather than `adapters/instagram` (§8.2). |
| 26 | Generate a **new** social post from an existing listing (queue is dry) | 1 | 1 | 0 | **T2**, ceiling T1 | New copy nobody approved. PRD §5.6 already says "surfaced for one-tap approval" — consistent. |
| 27 | Reply to IG/FB comments or DMs | 3 | 3 | 0 | **T3** | Same asymmetry as 17. |
| 28 | Email marketing | — | — | — | **CUT** | There is no list, no provider, no consent record, and CAN-SPAM/GDPR obligations attach on day one. Not "deferred" — **dropped**, until the operator says a list exists. |
| 29 | Create an Etsy sale / coupon | 1 | 1 | 2 | **T2**, hard discount cap | Giving away margin is spending money in the direction that doesn't show up on a card statement. §0 P6 unverified. |
| 30 | Change the shop's SEO/tag *strategy* (i.e. many listings at once) | 1 | 1 | 1 | **T2** + portfolio cap | Individually T1-ish; **in bulk it is the search-manipulation pattern**. The portfolio cap (§5) is the real control here, not the tier. |

### 3.4 Paid acquisition

| # | Capability | R | A | M | Tier | Defence |
|---|---|---|---|---|---|---|
| 31 | Etsy Ads on/off or budget change | 2 | 1 | 2 | **T2** ceiling — **out of v1** | §0 P5: likely no public API. If so this is a dashboard job forever. |
| 32 | Meta ads (create, budget, targeting) | 2 | 1 | 2 | **T2** ceiling — **out of v1** | App review + business verification + real spend. This is a milestone, not a capability. |
| 33 | **Any** budget increase | — | — | ≥2 | **T2 minimum, always** | Falls out of the rule. Worth writing down so nobody re-derives it. |

### 3.5 Business analysis

| # | Capability | R | A | M | Tier | Defence |
|---|---|---|---|---|---|---|
| 34 | "What sold / what's dead / seasonality" report | 0 | 0 | 0 | **T0** | This is the *actual* daily work of a business manager and it is the safest thing on the list. **Highest value-to-risk ratio in the document.** |
| 35 | "Shoot more of X" recommendation, stated in the brief | 0 | 0 | 0 | **T0** | Words on a page for the artist. |
| 36 | **Feeding that into the scoring tuning profile** | 1 | 0 | 0 | **T2** | Formula says T1. **Overridden**: a weight change silently alters what reaches Gate 1 — it edits the operator's creative queue without telling them. That deserves a tap. This is M7's territory anyway. |
| 37 | Pricing-strategy recommendation (stated, not applied) | 0 | 0 | 0 | **T0** | Applying it is #9. |
| 38 | Compliance drift check (AI-disclosure line present, production-partner declared) | 0 | 0 | 0 | **T0** to detect, **T2** to fix | Fixing = a listing edit = #10. |
| 39 | Health watchdog (sync failed, views went to zero, spend near cap) | 0 | 0 | 0 | **T1** | Notify loudly. Also the thing that catches M8 itself misbehaving. |

**Tally: 15 capabilities at T0/T1, 10 at T2, 9 at T3, 2 cut.** The operator's
"AI as shop owner" is *mostly* T0 analysis and T1 hygiene, with a T2 queue for
anything that moves money or public text, and a hard T3 wall around every human
being who is not the operator.

---

## 4. M7 is the prerequisite — what M8 can and cannot do without it

M7 (PRD §10) = *"Feedback loop v1: tuning profiles + weekly action queue."*

**What M8 cannot do before M7 exists:**

- **Any causal claim.** "Retitling this listing raised views 40%" requires a
  controlled comparison M7 owns. Without it, every T2 proposal's *reason* is
  correlational and the operator will (correctly) stop trusting the queue.
- **Anything that writes a tuning profile** (#36). That's literally M7's
  deliverable; M8 must consume it, never duplicate it.
- **Format-mix and mockup-style optimisation** (#11) — needs M7's attribution.
- **Price optimisation** (#9). Price changes without a demand signal are
  guessing with the operator's revenue.
- **A trustworthy "dead listing" definition.** M7 establishes what normal looks
  like. Before that, "dead" is a hand-set threshold in config — which is fine,
  but it must be *labelled* as hand-set, not presented as an insight.

**What is genuinely independent of M7 — and this is the entire v1:**

1. **The time series itself** (`proj_listing_daily`). It is a rebuild over events
   we already have. It is also *the input M7 needs*, so building it in M8a
   accelerates M7 rather than duplicating it. Arguably this projection should be
   pulled forward into M7 and M8 should consume it — see §17.3.
2. **Descriptive analytics.** "What sold, what didn't, revenue by day, which
   subjects convert" is `GROUP BY`, not inference. No feedback loop required.
3. **The autonomy chassis** — registry, tiers, ladder, caps, kill switch, action
   ledger, undo, brief. Entirely mechanical. Independent of every data question.
4. **Listing hygiene on non-inferential grounds** (#5/#6/#7): "this listing has
   had zero views and zero sales in 8 months" needs no model.

So: **M8a is buildable before M7 and is worth building before M7; M8b is not.**
Sequencing recommendation in §17.3.

---

## 5. Money, irreversibility, caps, kill switches, audit

**Nothing spends money by default.** `ops.monthly_spend_cap_usd` ships at
**`0.00`**. Autonomy literally cannot spend until the operator types a number
into config. Listing fees ($0.20 renewals) are counted against it, so even
turning auto-renew *on* is metered.

| Control | Default | What it stops |
|---|---|---|
| `autonomy.enabled` | **`false`** | Everything. The master switch; merge dark (M2b decision 5 precedent). |
| `daily_action_cap` (global) | 10 | A loop executing 500 actions overnight. |
| `per_capability_daily_cap` | 5 | One capability monopolising the budget. |
| `weekly_catalog_pct_cap` | 10% | The *portfolio* failure: 40 individually-defensible deactivations that empty the shop. **This is the control the tier system does not provide.** |
| `monthly_spend_cap_usd` | **20.00** *(operator, 2026-08-04)* | All spend. See the budget note below — the default is no longer 0.00. |
| `proposal_ttl_days` | 14 | A stale T2 proposal executing against data from a month ago. |
| `llm.monthly_soft_cap_usd` | existing $10 | Shared with vision + copy. The brief is SQL in v1, so it adds $0. |
| Triple gate | off / off / absent | Live writes: `--live-autonomy` + `SHOPSTEWARD_LIVE_AUTONOMY=1` + Etsy tokens with the needed scope. M5a §9 pattern, unchanged. |
| `ops.halted` event | — | An in-band kill switch: a UI/CLI button appends `ops.halted`; every capability checks it before `execute()`. Survives a run already in progress. |
| No daemon | — | The runner is `shopsteward ops run`, invoked on demand or by Windows Task Scheduler (decision 24: on-demand, no daemon). **Disabling the scheduled task is the belt-and-braces kill switch that does not require our code to work.** |

**BUDGET (operator, 2026-08-04): $20/month for the first three months, then 30%
of profits.** Allocation decision, delegated and taken:

- **100% to listing fees, 0% to advertising, for the full three months.** Etsy
  charges $0.20 per listing per 4-month cycle, so $20/mo buys roughly 100 new
  listings and the budget is fully consumed at that rate. Etsy Ads at this
  budget on a thin catalog buys impressions largely against the shop's own
  listings; the leverage is catalog depth from already-shot photographs, not
  paid traffic to a hundred listings.
- **No ad spend without a fresh operator decision.** §2.2's Money axis makes any
  budget increase T2 minimum by construction, and paid acquisition (#31/#32)
  stays out of v1 regardless.
- **"30% of profits" is not computable today.** Profit requires the receipt and
  cost data the M8a brief assembles (§7 slice 1). The budget formula is
  therefore a hard dependency on M8a, not merely a motivation for it —
  flag at the three-month mark if the brief does not exist.
- Listing fees are metered against the cap like any other spend, so the Brief's
  spend-vs-cap line (§6) doubles as a listing-rate governor: hitting the cap
  means the catalog grew 100 listings that month, which is information worth
  surfacing rather than a failure.

**Audit trail — append-only, attributable, reviewable.** Every action produces a
chain, and the chain is the point:

```
action.proposed  {action_id, capability, target{type,id}, tier, reason,
                  inputs_hash, estimated_cost_usd, undo_available, expires_at}
action.approved  {action_id, by:"operator"|"tier:T0"|"tier:T1"}   <- ALWAYS present.
                  A T0 auto-approval is recorded explicitly, so the log never
                  contains an execution with no visible authorisation.
action.refused   {action_id, reason:"daily_cap"|"portfolio_cap"|"budget"|"halted"
                  |"policy_unverified"|"precondition"|"expired"}
action.executed  {action_id, before:{...}, after:{...}, cost_usd, duration_ms}
action.failed    {action_id, stage, error:{code,message}}
action.rejected  {action_id, by:"operator", reason?}
action.undone    {action_id, restored_to:{...}}
capability.promoted / .demoted  {capability, from_tier, to_tier, trigger}
ops.halted / .resumed           {reason}
```

`before`/`after` are what make `undo()` possible **and** are the audit record —
one payload, two jobs. Rules carried forward: **no token, key, or signed URL ever
enters an event payload** (decision 35, decision 48); a **refusal is an event**,
so "why didn't it do the thing" is answerable from the log, not from guesswork.

---

## 6. The morning after a fully autonomous night

The operator opens `http://localhost:8000/brief` (or `shopsteward ops brief`)
and sees exactly this, in this order:

```
ShopSteward — Tuesday 4 August                       autonomy: ON   halt ▸

⚠  NEEDS YOU (2)
   • Etsy sync failed 03:12 — token refresh returned 401.  [re-auth]
   • "Sandhill Cranes at Dawn" — 412 views, 0 sales in 60d.
     Proposed: reduce $24 → $19 (T2, price).  [approve] [reject] [why?]

✅ DONE OVERNIGHT (3)          — every one of these has Undo for 7 days
   • auto-renew OFF ×3: "Foggy Pines", "Red Rock 4", "Barn Owl II"
     reason: 0 views + 0 sales in 241d, renewal due in 6d.  [undo all]
   • Posted to Instagram + Facebook: "Elk at Sunset" (Gate-3 approved 08-01).

⛔ REFUSED (2)
   • deactivate ×4 — portfolio cap (10%/week) already used.  retry Monday.
   • Etsy Ads budget — spend cap is $0.00. Set one to enable.

📈 THE SHOP
   7d revenue $148 (+22% vs prior 7d) · 4 orders · 61 active listings
   Selling:   sunrise/landscape 3 of 4 orders; 16×20 framed is 2 of 3 physical
   Dying:     11 listings, 0 views in 90d (7 are "Winter" — seasonal, not dead)
   Shoot more: "waterfall, long exposure" — 3 of your top 5 by views, 1 listing
   Watch:     digital conversion 2.1% (was 3.4%) — 3 weeks running

🪜 AUTONOMY
   listing.autorenew_off   T2 → T1 promoted (21 approvals, 0 rejections, 19d)
   listing.deactivate      T2  (6 of 20 approvals, 12d) — needs 14 more
   social.crosspost_fb     T2  (0 of 20)
   spend this month: $0.60 of $0.00 cap → CAPPED, 2 renewals skipped
```

Design constraints on this screen, which are load-bearing:

- **The refusals are as prominent as the actions.** A system that only shows what
  it did is a system whose caps are invisible until they surprise you.
- **Every "done" row carries Undo and a one-sentence reason.** If a reason can't
  fit in a sentence, the capability shouldn't be above T2.
- **The ladder is visible.** The operator can see autonomy being earned, which
  is the only thing that makes granting it feel safe.
- **It is one screen and it is read-only by default.** Approving is a tap;
  reading requires no taps.

---

## 7. Smallest genuinely useful v1 — **M8a**

The requested scope is five milestones of work. Most of it should not be built
now, and some of it should never be built. **M8a is four slices, ≈2 weekends**,
and it is the version worth shipping:

**M8a = the Brief + the chassis + exactly one write.**

1. **`proj_listing_daily`** — a per-listing daily time series rebuilt from
   `etsy.listing.observed` events already in the log. No API calls, no new
   scopes, no new dependency.
2. **The Brief** — deterministic SQL and a template. **No LLM.** "What sold,
   what's dead, what's trending, what needs attention" is a `GROUP BY`, not a
   language model, and an LLM narrating thin data produces confident nonsense
   that costs trust. *LLM narration is a later, optional, T0 nicety — and it
   makes sense only after M7 gives it something to reason about.*
3. **The autonomy chassis** — registry, tier engine, ladder, caps, kill switch,
   action ledger, undo, refusal accounting. Fully exercised against fakes.
4. **One capability: `listing.autorenew_off` / `_on`** (#5/#6), shipped at T2,
   `max_tier` T1. One field, reversible for months, saves real money, touches no
   customer, needs **no new Etsy scope** (`listings_w` is already consented).

**M8a deliberately excludes** — and this list is the design, not an apology:

all customer contact of any kind (§3.2 is entirely T3); all paid acquisition;
all price changes; all title/tag/description edits on live listings; deactivation
(#7 — one slice later, once the chassis has one capability's worth of scar
tissue); relisting/renewal purchase (#8, policy-blocked); Facebook (waits for
M6's adapter shape); email (dropped); coupons/sales; inventory (does not exist);
POD order monitoring (highest-value *next* thing, but it is a new external
integration); bundles (catalog still too small — M5a/M5b both said so); any
tuning-profile write-back (M7 owns it); LLM narration; any daemon.

---

## 8. Architecture

### 8.1 Module map

```
src/shopsteward/pipeline/ops/__init__.py
src/shopsteward/pipeline/ops/models.py        ProposedAction, ExecutionResult, Tier,
                                              CapabilitySpec, Brief, BriefSection, OpsReport
src/shopsteward/pipeline/ops/config.py        ops.json load/hash/seed/get  (listings/config.py twin)
src/shopsteward/pipeline/ops/registry.py      Capability Protocol + REGISTRY; enforces the §2.3
                                              invariants at import time (no undo => cannot exceed T2)
src/shopsteward/pipeline/ops/tiers.py         PURE: effective_tier(capability, state, config),
                                              ladder promote/demote arithmetic
src/shopsteward/pipeline/ops/governor.py      PURE-ish: caps, budget, portfolio %, halt check,
                                              proposal TTL -> approve | refuse(reason)
src/shopsteward/pipeline/ops/runner.py        Orchestrator: propose -> govern -> execute -> event.
                                              Idempotent by action_id.
src/shopsteward/pipeline/ops/analytics.py     PURE SQL over proj_listing_daily + proj_sales:
                                              selling/dying/trending/seasonality buckets
src/shopsteward/pipeline/ops/brief.py         Assemble the Brief from analytics + proj_actions +
                                              proj_capability_state. Template, no LLM.
src/shopsteward/pipeline/ops/capabilities/autorenew.py    the ONE v1 capability
src/shopsteward/pipeline/ops/projections.py   proj_listing_daily, proj_actions,
                                              proj_capability_state, proj_ops_config;
                                              rebuild_ops()
src/shopsteward/pipeline/ops/api.py           APIRouter /api/ops (brief, approve, reject, undo, halt)
src/shopsteward/pipeline/ops/cli.py           shopsteward ops brief|run|approve|halt|status
config/defaults/ops.json                      schema shopsteward.ops/1
frontend/src/pages/Brief.tsx                  the §6 screen; App.tsx "Brief" tab
tests/pipeline/ops/...
```

**No new import-linter contract** — `ops` lives inside `pipeline`, the M5b/pod
precedent. The editing boundary is untouched. **No new Python dependency.**

### 8.2 Adapters

- **`adapters/etsy`** — extend the *read* Protocol with
  `list_listings(state: str = "active")` (today it is hardcoded to
  `/listings/active`, so expired/inactive listings are invisible) and
  `get_listing(listing_id)`. Extend `EtsyListingUpdate` to carry
  `should_auto_renew` and `state`. **No new scope** for M8a. Every scope beyond
  that is §17.9.
- **`adapters/meta`** — **an architectural note delivered before M6 is built,
  which is why it is worth saying now.** The operator's own observation is right:
  one Graph API covers Instagram Business and Facebook Pages, and IG publishing
  requires a Business/Creator account linked to a Page. **M6 should therefore
  create `src/shopsteward/adapters/meta/` with an `IgPublisher` and a
  `PagePublisher`, not `adapters/instagram/`.** Renaming later costs an
  import-linter contract amendment and a §8.2 review; naming it right costs
  nothing today. (PRD §7.1 and CLAUDE.md both currently say
  `adapters/instagram` — flagging the discrepancy.)
- **No new adapter for analysis.** The brief is SQL. If LLM narration is ever
  authorised it reuses `adapters/copy`'s OpenRouter transport with a different
  strict schema — no new provider, no new decision.
- **No adapter for the ops runner itself.** It is core orchestration.

### 8.3 Events and projections

Events: the `action.*` / `capability.*` / `ops.*` set in §5, plus
`opsconfig.seeded` / `.updated`, plus `brief.generated {run_id, date, sections}`.
Dot-separated, past tense, immutable, `user_id` on every row.

Projections (drop-and-rebuild via `rebuild_ops()`, `user_id` on every table — **no
migration**):

- `proj_listing_daily(user_id, listing_id, day, views, num_favorers, price_usd,
  state, PK(user_id, listing_id, day))` — folded from `etsy.listing.observed`
  bucketed on `events.created_at`, last observation within a day wins.
- `proj_actions(user_id, action_id PK, capability, target_type, target_id, tier,
  state proposed|approved|rejected|refused|executed|failed|undone, reason,
  inputs_hash, cost_usd, before_json, after_json, proposed_at, resolved_at)`.
- `proj_capability_state(user_id, capability PK, tier, approvals, rejections,
  undos, executions, tier_since, last_action_at)`.
- `proj_ops_config(user_id, name PK, config_json)`.

**Idempotency:** `action_id = sha256(capability | target_id | inputs_hash |
ops_config_hash | day)`. Re-running `ops run` on the same day re-proposes
nothing. A proposal that already has a terminal event is never re-proposed. A
changed `ops.json` changes the hash and legitimately re-proposes — the M4/M5a
`config_hash` pattern throughout.

---

## 9. Implementation slices (dependency order)

| # | Scope | Size | Mergeable with tests |
|---|---|---|---|
| **0** | **NO CODE. Precondition.** Answer §0 P1–P10 from the sources, and record the answers in the PRD decision log. Slices 3+ are unsafe to design without P3 and P7. | 1 evening of reading | — |
| **1** | `proj_listing_daily` + `ops/analytics.py` + `ops/config.py` + `config/defaults/ops.json` + `shopsteward ops brief` (text) — **read-only, no writes, no chassis.** Rebuild over existing events; ships value on day one with zero risk. | 1 evening | analytics unit tests on a seeded event log |
| **2** ⭐ | **FIRST PR of the milestone → mandatory §8.2 review.** The chassis: `registry.py`, `tiers.py`, `governor.py`, `runner.py`, the `action.*` event set, `proj_actions`, `proj_capability_state`, kill switch, caps, triple gate, `ops run --dry-run`. **Zero capabilities registered.** | 1 weekend | cap/refusal/ladder/halt tests against a stub capability |
| **3** | Capability #1: `listing.autorenew_off` / `_on` (`max_tier=T1`, ships T2) + `EtsyWriteAdapter`/`EtsyAdapter` extensions (`state` filter, `should_auto_renew`) + `FakeEtsyWriteAdapter` support + `undo()`. | 1 evening | full fake-Etsy E2E incl. undo restoring state exactly |
| **4** | `Brief.tsx` + `/api/ops` (brief, approve, reject, undo, halt) — the §6 screen. | 1 evening–weekend | API tests; UI is the decision surface (Gate 1/Gate 3 precedent) |
| **5** | *(gated, operator-approved §8.4 smoke)* One listing on the real shop: flip `should_auto_renew` false, verify in the Etsy UI, undo, verify restored. Record + scrub fixtures. | 1 evening | — |
| — | **M8a ENDS HERE.** | **≈2 weekends** | |
| **6** | *(M8b)* `listing.deactivate` (#7) + portfolio-cap exercise. | 1 evening | |
| **7** | *(M8b, needs a provider order API = new external service)* POD order watchdog (#14) at T1. | 1 weekend | |
| **8** | *(M8b, needs M6's `adapters/meta`)* Facebook cross-post (#25) at T2→T1. | 1 evening | |
| **9** | *(M8c, needs M7)* Price / title / tag iteration (#9, #10) at T2, proposals sourced from M7's attribution. | 2 weekends | |
| — | Everything else in §3 is deferred or dropped. | | |

---

## 10. Guardrail impact

| Guardrail | Impact |
|---|---|
| **Three gates** | **Preserved, and explicitly not extended.** M8 adds no creative touchpoint. The Brief is a *shop* surface, not a *hero* surface; a hero never waits on it. Capability #16 (draft building) routes through the existing Gate 3 rather than inventing a new approval. |
| **Monolithic core / pluggable adapters** | `ops` is core orchestration under `pipeline/`; every external call goes through `adapters/etsy` (extended) and, later, `adapters/meta`. No SDK anywhere; httpx only. |
| **Editing-module boundary** | Untouched. `ops` imports nothing from `editing/`, and `editing/` cannot see `pipeline/`. No import-linter amendment in M8a. |
| **Landing-folder handoff** | Not involved. |
| **Event-sourced SQLite** | Append-only throughout; the action ledger *is* the audit trail. Four new projections, all drop-and-rebuild, **no migration**. `undo` appends `action.undone` — it never deletes an event. |
| **Configuration over code** | Every threshold, cap, dead-listing definition and ladder constant in `ops.json`, DB-seeded with an `ops_config_hash`. **Exception, deliberate:** `max_tier` is in Python. Config may only lower a capability's ceiling, never raise it. |
| **POD-first** | Unchanged. #9b makes it explicit that POD price edits stay forbidden (decision 43). |
| **AI never touches the photograph** | Not exercised. M8a contains no model at all. |
| **`user_id` on every major table** | All four new projections carry it. |
| **No live external APIs in tests** | Everything runs on `FakeEtsyWriteAdapter` and a stub capability; slice 5 is the only network and it is separately operator-gated. |
| **PRD §3.2** | **VIOLATED by the requested scope** — see §0(c). M8 requires a PRD amendment or an explicit v2 designation. §17.3. |
| **Nights-and-weekends** | M8a is 2 weekends. The other 30 capabilities are named, tiered, and *not built*. |

---

## 11. Smallest test that proves it works

`tests/pipeline/ops/test_e2e_autonomy.py` — one test, entirely on fakes, zero
network:

Seed three listings with 240 days of synthetic `etsy.listing.observed` events (two
dead, one healthy) and an `ops.json` with `daily_action_cap=1`. Then assert, in
order:

1. `autonomy.enabled=false` → `ops run` produces **zero** `action.executed`.
2. Enabled, capability at T2 → two `action.proposed`, **zero** executed; the
   healthy listing is not proposed.
3. Approve one → executed; `FakeEtsyWriteAdapter` shows `should_auto_renew=false`
   on exactly that listing.
4. Approve the second → `action.refused{reason:"daily_cap"}`, **not** executed.
5. `undo` → `action.undone`, and the fake's listing state is **byte-identical**
   to its pre-execution snapshot.
6. That undo → `capability.demoted` and both ladder counters reset to zero.
7. Re-run → `skipped_idempotent`, zero new proposals, zero new adapter calls.
8. Assert **every** `action.executed` in the log has a preceding
   `action.approved`, and no event payload anywhere contains a token or key.

If the tier engine, the governor, the ladder, the undo path, idempotency, or the
audit invariant breaks, that one test fails.

---

## 12. Rollback criteria

**The lever:** `ops.autonomy.enabled=false` in config → the runner no-ops
entirely; the Brief still renders (it is read-only). No schema to unwind, no
events to delete. Second lever, outside our code: delete the scheduled task.

**Revert the milestone if any of:**

- (a) an `action.executed` appears with no preceding `action.approved`;
- (b) a capability executes above its `max_tier`;
- (c) an `undo()` fails to restore the recorded `before` state, once;
- (d) spend exceeds `monthly_spend_cap_usd` by any amount;
- (e) Etsy sends any policy warning, rate-limit block, or account notice
  temporally coincident with an autonomous run;
- (f) the portfolio cap is reached in a week the operator did not intend a bulk
  change (i.e. the system wanted to change >10% of the catalog — that is a
  signal the *proposals* are wrong, not that the cap is too low);
- (g) the operator rejects more than 50% of T2 proposals from any capability
  over 20 proposals — the recommendations are noise and the ladder is measuring
  the wrong thing.

---

## 13. Risks

### 13.1 Account suspension — the existential one

Etsy can close a shop. A closed shop ends the business, and no amount of saved
time offsets that. The specific exposures, in descending order:

- **Programmatic renew/relist** (#8) to reset search freshness — this is the
  classic search-manipulation pattern and §0 P3 must be answered before a line of
  code. *Mitigation: #8 is not in M8a and is flagged policy-blocked.*
- **Automated buyer messaging** (#17) — §0 P1/P2. *Mitigation: T3, wall.*
- **Bulk listing edits** tripping rate limits or looking like churn (#30).
  *Mitigation: portfolio cap, not just the tier.*
- **API ToU compliance generally** — dormant-app bans, personal vs commercial
  access (decision 35 notes both). An M8 that goes quiet for 6 months is a
  different failure than one that goes too loud.

*Standing mitigation: every capability records the policy question it depends on,
and `governor.py` refuses any action whose `policy_verified` flag is false in
config. Policy verification is a config value the operator sets after reading —
not an architect's assumption.*

### 13.2 Customer-trust damage — the reputational asymmetry, as arithmetic

This is the argument for the entire §3.2 wall, and it is not squeamishness:

A small shop's public rating is a **small-n average**. At 20 reviews averaging
5.0, a single 1-star drops it to **4.81 and posts a permanent, visible negative
comment at the top of the shop**. Etsy's own surfaces amplify low ratings.

Now price the upside. Fifty competent automated replies produce, at best, a
marginal improvement in response time — a metric with no established conversion
effect at this scale, on a shop with a handful of orders a week. **The expected
value of automated customer contact is negative and it is not close.** One bad
message costs more than fifty good ones save, arithmetically, because the
downside is a permanent public artefact and the upside is an unmeasurable
increment.

The same asymmetry applies to review responses (#19), refunds (#20), and
disputes (#21). It does **not** apply to a listing edit, which no named human
receives — which is precisely why the Audience axis exists in §2.2 and why it
alone can force T3.

*Second-order risk:* the *assist* path (#18) launders the risk back in through
human factors. A fluent, ready-to-send draft addressed to a real customer will
eventually be sent unread. Hence the override to "bulleted context, not
prose" — or better, nothing.

### 13.3 Runaway spend

Real exposure in M8a is **$0.20 listing fees**, and even that is metered against
a cap defaulting to `$0.00`. The genuine spend risks are Etsy Ads (#31) and Meta
ads (#32), both out of v1 and both possibly not even API-reachable. The
structural protection is §2.2's Money axis: **a budget increase can never reach
T0 or T1.** The residual risk is an operator who raises the cap once "to test"
and forgets — mitigated by the Brief printing spend-vs-cap every single morning,
including when it is zero.

### 13.4 The portfolio failure — individually right, collectively wrong

Forty deactivations, each individually defensible on its own thresholds, that
together empty the shop. **Tiers do not catch this; only the
`weekly_catalog_pct_cap` does.** This is the failure mode most likely to
actually occur, because it requires no bug — just a threshold that is slightly
off and a system that is working exactly as designed.

### 13.5 Trust decay and ladder gaming

If T2 proposals are noisy, the operator rubber-stamps them, the ladder promotes
on inattention, and the system becomes autonomous through boredom. Mitigations:
the elapsed-time requirement (a batch of approvals in one sitting cannot
promote), instant asymmetric demotion, and revert criterion (g). Inverse risk:
proposals so conservative they are never worth reading, and the Brief becomes
unopened mail — which is a product failure even though nothing broke.

### 13.6 Bad data driving good machinery

A failed sync returning zeros makes every listing look dead. *Mitigation:*
capabilities declare preconditions (`last_successful_sync < 48h`,
`sample_days >= N`), the governor refuses on precondition failure, and the
refusal appears in the Brief — the §6 mock shows exactly this case.

---

## 14. Non-goals (explicit)

Customer service of any kind (messages, reviews, refunds, disputes, shipping
questions — the whole of §3.2); review solicitation; email marketing (no list, no
provider, no consent record); paid acquisition on any platform; inventory
management (**does not exist** for digital-999 and made-to-order POD); order
placement or fulfilment execution; a bundle proposer (catalog still too small,
per M5a §11 and M5b §11); tuning-profile write-back (M7 owns it); LLM narration
of the brief in v1; any daemon or long-running service; a scheduler of our own;
multi-shop or multi-tenant operation; Pinterest, TikTok, or any channel beyond
Etsy + Meta; predictive/forecasting models; automated A/B testing of live
listings; any capability that cannot state its reason in one sentence; any
capability without a working `undo()` above T2.

---

## 15. Rejected alternatives

- **A fourth gate ("Gate 4 — Operate").** Gates are per-hero and synchronous
  with creation; ops actions are per-shop and continuous. Routing ops through
  Gate 3 means a business action cannot happen unless a photograph is publishing
  that day, and it pollutes the creative surface with commerce. Separate axis,
  separate surface.
- **A single autopilot on/off switch.** Collapses "write a report" and "refund a
  customer" into one decision. The operator would rationally leave it off
  forever, and the milestone would deliver nothing.
- **Approve everything (per-action confirmation).** Not a fourth gate — an
  unbounded stream of interruptions, strictly worse than a gate, and precisely
  what the operator asked to be relieved of.
- **An LLM agent with tool access and free rein.** Unbounded action space; cannot
  be tested offline (violating the hard guardrail); "budget cap" is
  unenforceable when the action set is not enumerable; and no `undo()` can be
  guaranteed for an action nobody wrote. The capability registry exists
  *because* the action space must be finite and each member must carry its own
  reversal.
- **Tiers assigned by judgement per capability.** Guarantees re-litigation on
  every addition. The §2.2 derivation rule makes placement mechanical and makes
  the two corollaries (money ≥ 2, named human ≥ 2) unarguable.
- **Tiers configured in `ops.json` including the ceiling.** Config-over-code is a
  rule about *tuning*, not about *safety limits*. A config edit must not be able
  to make buyer messaging autonomous. `max_tier` stays in Python; config can only
  restrict.
- **An LLM writing the brief in v1.** "What sold, what's dead, what's trending"
  is a `GROUP BY`. An LLM narrating thin data produces confident nonsense, costs
  money against the $10 cap, and cannot be asserted in an offline test. Narration
  is a post-M7 nicety.
- **A `BusinessManagerAdapter` / `AnalyticsAdapter` Protocol.** Interface with
  one implementation; the M2b `technical.py` precedent settles that deterministic
  local computation is a plain module here.
- **A daemon or an in-process scheduler.** Decision 24's on-demand rule stands.
  Windows Task Scheduler calling the CLI is boring, external, and gives a kill
  switch that does not depend on our code being correct.
- **Reusing `proj_listings` for the time series.** It is last-write-wins and
  destroys history. A separate `proj_listing_daily` is a pure rebuild over
  events we already hold — and `proj_listings` keeps its existing contract.
- **Storing the brief as a file/markdown artefact.** It is a projection of the
  event log; regenerate it, never store it. Storing it invites editing it.
- **Deleting or archiving old action events to keep the log small.** Append-only.
  The audit trail's value is that it is complete.
- **Building customer messaging behind a very careful prompt.** The prompt is not
  the risk; the asymmetry in §13.2 is, and no prompt changes arithmetic.

---

## 16. PRD §13 decision-log candidates (70–78)

```
M8 design (DRAFT 2026-08-03, NOT APPROVED; draft spec at
docs/designs/2026-08-03-m8-autonomous-operations-draft.md):

70. The three gates govern the CREATIVE path only. Post-publish shop operations
    are governed by a second, orthogonal control: four autonomy tiers -- T0 Auto
    (do it, log it), T1 Auto+Notify (do it, surface it with one-click undo), T2
    Propose (prepare everything, one tap approves), T3 Operator-only (never
    automated; an assist artefact is permitted, the act is human). There is NO
    fourth gate: ops actions are per-shop and continuous, heroes are per-photo
    and synchronous, and routing ops through Gate 3 would make a business action
    wait on a photograph.
71. A capability's tier is DERIVED, not chosen: tier = max(reversibility,
    audience, money) on the three 0-3 scales in the design's 2.2. Two corollaries
    are therefore structural and not open to case-by-case argument: a budget
    increase can never be T0 or T1 (money >= 2), and anything a named human
    being reads can never be T0 or T1 (audience >= 2). Each capability also
    declares a max_tier CEILING in Python, not config; configuration may only
    lower a ceiling, never raise it.
72. Autonomy is EARNED, never asserted. Every capability's first live execution
    is T2 regardless of ceiling. Promotion T2->T1 requires >=20 operator
    approvals AND zero rejections AND >=14 elapsed days (the clock defeats
    promotion-by-batch-approval); T1->T0 requires >=30 executions, zero undos,
    >=30 days. ONE rejection or ONE undo demotes immediately and resets both
    counters. A capability without a working undo() cannot be registered above
    T2 -- enforced at import time, not by review.
73. Customer communication is T3 across the board: buyer messages, review
    responses, refunds, disputes, shipping questions, review solicitation. The
    reason is arithmetic, not caution: on a small-n public rating one 1-star
    review is a permanent visible artefact worth more than fifty competent
    automated replies save. A ready-to-send AI-drafted reply is ALSO refused,
    because a fluent draft addressed to a real customer will eventually be sent
    unread; only bulleted context is permitted, and not in v1.
74. Etsy's policy on automation is an UNRESOLVED EXTERNAL CONSTRAINT and a
    precondition, not an assumption. Ten questions (design 0(b)) must be answered
    from Etsy's and Meta's own current documentation before the capabilities that
    depend on them are designed -- notably whether the v3 API exposes buyer
    messaging at all, whether programmatic renew/relist is search manipulation,
    whether an Etsy Ads API exists, and which scopes each write needs. Each
    capability records the policy question it depends on and the governor REFUSES
    any action whose policy_verified flag is false.
75. Nothing spends money by default: ops.monthly_spend_cap_usd ships at 0.00, so
    autonomy cannot spend until the operator types a number. Additional bounds:
    a global daily action cap, a per-capability daily cap, a rolling
    weekly_catalog_pct_cap (default 10% of active listings) that catches the
    portfolio failure tiers cannot see, a proposal TTL, an in-band ops.halted
    kill switch checked before every execute, the master autonomy.enabled=false
    default, and the existing live-write triple gate. The runner is on-demand
    (shopsteward ops run), never a daemon (decision 24); nightly operation is the
    OS scheduler's job, which makes "disable the task" a kill switch that does
    not depend on our code.
76. Every autonomous action produces a full append-only chain: action.proposed ->
    action.approved (ALWAYS present, including "tier:T0" self-approval, so no
    execution ever appears without visible authorisation) -> action.executed
    {before, after} | .refused{reason} | .failed | .rejected | .undone.
    before/after is both the undo payload and the audit record. A REFUSAL is an
    event, so "why didn't it act" is answerable from the log. Tokens, keys and
    signed URLs never enter a payload (decisions 35, 48).
77. M8's smallest useful version (M8a, ~2 weekends) is: proj_listing_daily
    rebuilt from the etsy.listing.observed events ALREADY in the log (the sync
    appends one per listing per run; proj_listings discards the history with
    INSERT OR REPLACE) -- costing zero API calls and zero new scopes; a
    deterministic SQL-and-template daily Brief with NO LLM (what sold / what is
    dead / what is trending is a GROUP BY, and a model narrating thin data
    produces confident nonsense against a $10 cap); the full autonomy chassis;
    and EXACTLY ONE write capability, listing auto-renew off/on -- one field,
    reversible for months, saves real money, touches no customer, and needs no
    new Etsy scope. Everything else in the requested scope is deferred, and
    inventory management and email marketing are DROPPED (digital quantity is 999
    and POD is made-to-order, so no inventory exists; there is no email list,
    provider, or consent record).
78. M7 is a hard prerequisite for the inferential half of M8. Before M7 exists,
    M8 may not change prices, iterate titles/tags, optimise format or mockup mix,
    or write a tuning profile -- every proposal's "reason" would be
    correlational and the approval queue would lose the operator's trust.
    Descriptive analytics, listing hygiene on non-inferential grounds, and the
    chassis itself are genuinely M7-independent. Separately: a capability whose
    output is an UNPUBLISHED Etsy DRAFT (restock in a new format, bundle,
    prepared refresh) needs no autonomy tier at all -- Gate 3 is already its
    approval.
```

---

## 17. OPERATOR DECISIONS REQUIRED

*Ordered by how much work each unblocks. Nothing in this document is assumed.
1–3 block essentially everything; 4–6 block M8a's shape; the rest block
individual capabilities.*

| # | Question | Blocks | Answer form |
|---|---|---|---|
| **1** | **Do you accept the four-tier model, the `max(R,A,M)` derivation rule, the Python-level `max_tier` ceiling, and the promotion ladder (§2)?** If the model is wrong, the placement table and everything downstream is wrong. | The entire milestone | accept / amend / reject |
| **2** | **Will you read Etsy's current API Terms of Use and Seller Policy and answer §0 P1–P8?** Especially P1 (is buyer messaging in the API at all), P3 (is programmatic relisting search manipulation), P5 (does an Ads API exist), P7 (rate limits). This design will not be built against a guess. | Every write beyond auto-renew; possibly auto-renew | yes + the answers |
| **3** | **Milestone placement and the PRD conflict.** PRD §3.2 lists order management, customer service and Facebook as v1 non-goals — M8 contradicts it. Amend §3.2, or designate M8 as v2? And: does M8 sit after M7 (recommended), or does M8a's `proj_listing_daily` get pulled forward *into* M7, with M8 consuming it? | Build order, PRD | amend / v2 / defer, + M7-first yes/no |
| **4** | **Review §3's placement table row by row.** Which capabilities are you actually willing to have run unattended, and where do you disagree with the tier? This is the document's centrepiece; disagreement is expected on rows 7, 10, 11, 26 and 36. | The M8b/M8c roadmap | per-row |
| **5** | **Confirm M8a's scope (§7):** brief + chassis + auto-renew only, ~2 weekends. Or name what must be in v1 that isn't. | Slices 1–5 | confirm / amend |
| **6** | **Cap values:** `daily_action_cap` (10?), `per_capability_daily_cap` (5?), `weekly_catalog_pct_cap` (10%?), `monthly_spend_cap_usd` (**0.00** recommended), `proposal_ttl_days` (14?), ladder thresholds (20/14d, 30/30d?). | Slice 2 | six-plus values |
| **7** | **Customer communication: confirm the §3.2 wall (all T3, none built).** If you want any part of it, say exactly which and it will be re-costed against §13.2. | Whether §3.2 is a wall or a roadmap | confirm / specify |
| **8** | **Facebook: yes/no — and may M6 be named `adapters/meta` (IG + Pages) rather than `adapters/instagram`?** Naming it right now is free; renaming later costs an import-linter amendment and a §8.2 review. CLAUDE.md and PRD §7.1 both currently say `adapters/instagram`. | M6's adapter shape (decide **before** M6, not during M8) | yes/no + rename yes/no |
| **9** | **Etsy scopes.** M8a needs none beyond today's `listings_r listings_w transactions_r shops_r`. Any capability past auto-renew may need more (§0 P8). Is another `shopsteward etsy auth` re-consent round authorised in principle, or must each one come back to you? | Slices 6+ | per-round / blanket / no |
| **10** | **Paid acquisition: confirm out of v1** (Etsy Ads likely has no API; Meta ads need app review + business verification + real money). | #31, #32 | confirm / discuss |
| **11** | **Email marketing: confirm dropped** (no list, no provider, no consent record, CAN-SPAM/GDPR attach on day one). | #28 | confirm / you have a list |
| **12** | **Dead-listing definition.** Placeholder: zero sales AND zero views in 180 days, minimum 90 days of observation. What are your numbers? Note seasonality — "Winter" listings look dead in July. | #4, #5, #7 | thresholds |
| **13** | **Nightly scheduling:** Windows Task Scheduler calling `shopsteward ops run` (recommended — no daemon, external kill switch), or purely manual invocation? | Slice 4, the §6 experience | scheduler / manual |
| **14** | **POD order monitoring (#14)** — a stuck Gelato order means a buyer paid and received nothing. Not in your candidate list; it is arguably the highest-value item *not* on it. It needs a provider order API = a new external integration (§8.2). Priority? | M8b slice 7 | high / later / no |
| **15** | **Does the Brief ever get LLM narration?** v1 says no (SQL and a template). Post-M7, under the shared $10 cap, yes/no? | A later nicety | yes / no / post-M7 |
| **16** | **Bookkeeping/tax export (#13)** — receipts → CSV. T0, trivially safe, not in your list. Want it in M8a? | one small slice | yes / no |
| **17** | **C-Suite critique panel** (CTO/CFO/CMO/CPO/Chief Legal, PRD §8.3) before this design is finalised — Chief Legal in particular should see §0 and §13.1 before any code. Run it? | Finalisation | yes / no |

---

*End of draft. Nothing above is approved. The fastest path through it: answer
§17.1 (do you accept the model), then §17.2 (read Etsy's policy), then §17.4
(argue with the table). Slice 1 — the read-only Brief over data you already have —
is safe to build the moment §17.3 and §17.5 are settled, and it is the only part
of this document worth building without waiting for the rest.*
