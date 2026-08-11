# Handoff — Pillar 5: Autonomous AI Revenue Management (M8a)

**For:** the next session. **Written:** 2026-08-11.
**Status of everything else:** Pillars 1–4 (editing → digital + physical shop) are
built, merged, fixture-green (694 tests), and audited. This handoff is ONLY the
last, largely-unbuilt pillar: running the shop autonomously to maximize revenue.

## Read first
- `docs/audits/2026-08-10-capability-audit.md` — §"Pillar 5" (evidence) + §"P3"
  remediation R10–R15, and the 2026-08-11 status update.
- `docs/designs/2026-08-03-m8-autonomous-operations-draft.md` — the existing M8a
  design, **explicitly marked "NOT APPROVED, NOT A PROPOSAL TO BUILD YET."** §0 is
  a reality check; §0(b) lists capabilities that may be **illegal/non-existent as
  APIs**; §2 a tier model; §8 the chassis; §17 operator decisions. Treat as a
  starting hypothesis, not settled truth.
- `CLAUDE.md` — non-negotiables still apply (adapters behind interfaces, no live
  APIs by default, config-over-code, event-sourced, draft-only writes).

## Verified current state (from the audit — evidence, not assumption)
- **No autonomous entrypoint exists.** Entire app is operator-invoked CLI + human
  Gate 1/2/3. `grep` for scheduler/daemon/cron/agent-loop → none (only an HTTP
  paginator + folder poller). `ops/cli.py` explicitly: "No run/approve/halt/status".
- **PARTIAL — analytics ingestion.** `core/sync.py` + `pipeline/ops/analytics.py`
  + `proj_listing_daily` compute revenue/top-sellers/dead-listings (M8a slice 1,
  the ONLY built slice). Gated live read. **Ceiling:** `LiveEtsyAdapter.list_listings()`
  hits `/listings/active` — **active-only**; expired/sold/draft are invisible
  (audit R9). Fix this before any lifecycle action.
- **MISSING:** feedback loop (analytics→tuning; `tuning.py` is seed+read only),
  scheduler/runner, goal/policy object ("maximize revenue" exists nowhere),
  revenue actions (reprice-live / re-tag-SEO / renew / pause / gap-fill / A/B),
  autonomy guardrails (spend caps, kill-switch, tiers, rate limits, undo,
  proposal TTL), an `ops` HTTP surface / run|approve|halt|status, an action/audit
  projection, and the Meta/IG promotion loop (adapter exists at `adapters/meta/`
  but is imported by nothing — dead code).

## Hard prerequisite — verify platform policy FIRST (no assumptions)
Several requested capabilities may violate Etsy/Meta policy or lack public APIs.
Resolve draft §0(b) questions P1–P10 against LIVE Etsy/Meta docs before designing
actions around them:
- Programmatic relist/renew as search manipulation (P3).
- No public Etsy Ads API (P5). Coupon/sale write endpoints unverified (P6).
- Buyer messaging / customer service (P1/P2) — likely out of bounds.
- Meta/IG publishing + rate/spend policy (P9/P10).
Anything that fails policy is cut from scope, not worked around.

## Build shape (R11–R15) — do NOT build all at once
1. **R11 Autonomy chassis** — capability registry (each action self-classifies a
   tier), governor (spend caps + kill-switch + rate limits + undo/TTL), a
   runner/scheduler for unattended cadence, and a proposal/action event stream.
   Design §2 (tiers) + §8 (chassis) are the starting spec.
2. **R12 Goal/policy object** — start NARROW (one metric, e.g. revenue per active
   listing over a window) → ranked candidate actions. Not an open-ended optimizer.
3. **R13 Feedback loop** — wire `analytics.py` outputs → `tuning.py` writes (the
   PRD's promised, currently-no-op loop).
4. **R14 Revenue actions as governed ops** — reprice, SEO re-tag, renew, pause
   underperformer, gap-fill draft, A/B copy. Per the app's model, anything
   customer-visible should terminate in a **Gate-3 draft**, not a T0 auto-write.
   Etsy write primitives already exist (`adapters/etsy/interface.py`).
5. **R15 Promotion loop** — wire the dead Meta/IG adapter behind the governor +
   a spend/rate cap (blocked on P9/P10).
   **Prerequisite for all lifecycle actions:** R9 — make the Etsy read adapter see
   inactive/expired/draft listings, not just active.

## Reuse (don't reinvent)
Event-sourced SQLite + projections (`core/events.py`, `core/projections.py`);
`pipeline/llm_ledger.py` (monthly cost cap pattern); the live-gate pattern
(`pipeline/live_gate.py`); draft-only write invariant + Gate 3
(`pipeline/listings/gate3.py`); the editing-local look-cost ledger as a soft-cap
precedent.

## Key files
- `docs/designs/2026-08-03-m8-autonomous-operations-draft.md` (blueprint)
- `src/shopsteward/pipeline/ops/` (built: analytics/brief/projections; slices 2+ absent)
- `src/shopsteward/pipeline/tuning.py` (feedback-loop write target)
- `src/shopsteward/adapters/etsy/{interface,live}.py` (write primitives; active-only read)
- `src/shopsteward/adapters/meta/` (unwired promotion adapter)
- `src/shopsteward/pipeline/live_gate.py` (guardrail pattern to extend)

## Suggested first-session plan
1. **Policy-verification pass** (web/docs) → a short findings doc: for each R14/R15
   action, PERMITTED / RESTRICTED / PROHIBITED with a citation. Cut the prohibited.
2. **Brainstorm → spec** the chassis (R11) + one narrow objective (R12) + the
   feedback loop (R13) ONLY. Defer R14 actions to per-action specs behind the
   governor; defer R15 (Meta) until P9/P10 clear.
3. Get operator approval on the M8a scope (it's currently unapproved + PRD §3.2
   lists autonomy as a v1 non-goal — so either approve M8a or amend the PRD).
4. Fix R9 (active-only visibility) as the first concrete PR — small, unblocks
   lifecycle actions, safe.
Build subagent-TDD, one governed slice per PR, everything default-off + gated.

---

## Kickoff prompt (paste to start the next session)

> Resume ShopSteward on the **autonomy pillar (M8a — autonomous AI revenue
> management)**, the last unbuilt capability. Read
> `docs/handoffs/2026-08-11-autonomy-pillar-handoff.md`,
> `docs/audits/2026-08-10-capability-audit.md` (Pillar 5 + P3), and
> `docs/designs/2026-08-03-m8-autonomous-operations-draft.md` first — verify
> current state against the code, don't trust these docs blindly.
>
> This is a v2 program and needs approval + external-policy verification before
> building. Do it in this order, no assumptions:
> 1. Run a **platform-policy verification pass**: for every proposed revenue
>    action (reprice, re-tag/SEO, renew/relist, pause, gap-fill, A/B copy,
>    Meta/IG promotion, coupons/ads, buyer messaging) classify PERMITTED /
>    RESTRICTED / PROHIBITED against **live** Etsy & Meta API/policy docs, with
>    citations. Cut the prohibited. Write it to `docs/`.
> 2. Then **brainstorm → spec** the minimum viable autonomy: the chassis
>    (capability registry + governor with spend caps/kill-switch/rate-limits/undo
>    + a runner + an action/proposal event stream), ONE narrow objective
>    (revenue per active listing over a window), and the analytics→tuning feedback
>    loop. Defer individual revenue actions to per-action specs behind the
>    governor; defer Meta until its policy clears.
> 3. Fix **R9** (Etsy read adapter is active-listings-only) as the first small PR —
>    it's the prerequisite for any lifecycle action.
>
> Non-negotiables (CLAUDE.md): adapters behind interfaces, no live APIs by default
> (flag+key+gate), config-over-code, event-sourced, and anything customer-visible
> terminates in a **Gate-3 draft**, never an unattended live write. Reuse the
> existing event log, `llm_ledger` cost-cap pattern, live gates, and Gate 3.
> Present a design for approval before building; build subagent-TDD, one governed
> slice per PR, default-off. Stop and ask on any load-bearing question.
