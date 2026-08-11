# M8a Autonomy — Build-Ready Spec (chassis + one objective + feedback loop)

**Status: PROPOSAL FOR APPROVAL. No code until the §7 decisions are answered.**
**Written:** 2026-08-11. Converges the 2026-08-03 architect draft
(`docs/designs/2026-08-03-m8-autonomous-operations-draft.md`) onto verified
code + the policy pass (`docs/policy/2026-08-11-autonomy-platform-policy.md`).
The draft is the argument and the tier model; **this doc is what to build.**
Where they differ, this doc wins (verified reality); the draft's §2/§3/§5/§13
still hold and are cited, not repeated.

Scope is exactly what the kickoff asked for and nothing more:
**(1) the autonomy chassis, (2) ONE narrow objective, (3) the analytics→tuning
feedback loop.** Individual revenue actions beyond the first are deferred to
per-action specs behind the governor; Meta is deferred until its App Review /
Business Verification clears.

---

## 1. What changed since the 2026-08-03 draft (verified against code)

| Draft assumption | Verified reality | Consequence |
|---|---|---|
| R9 — `list_listings()` is **active-only** (`/listings/active`) | **Already fixed.** `live.py:66-75` uses `getListingsByShop` over `("active","expired")`, on `main` since `a385595`; `proj_listing_daily` already records per-listing `state` | The "R9 first PR" is **a no-op or a tiny widening**, not a blocker. See §6. |
| §0(b) P1–P10 policy questions open | **Resolved** (policy doc) | Cut list is now evidence-based, not cautious |
| IG limit 25/50 posts/24h | **100 posts/24h** | Only matters at M8b slice 8 (Meta) |
| `monthly_spend_cap_usd` default `0.00` (draft §5) vs `20.00` (draft §5 budget note, operator 2026-08-04) | **Conflict inside the draft itself** | §7 decision — pick one |
| Slice-1 analytics = thin | **Richer than described:** `revenue_window`, `top_sellers`, `viewed_not_sold`, `dead_listings` (with `min_observed_days` + data-quality guards), `trending`, product/size mix all built in `ops/analytics.py` | The objective (§4) and feedback loop (§5) already have their inputs |

**Policy cut list (do not automate — no API or barred):** coupons/sales, Etsy
Ads, buyer messaging, refunds/disputes/cases, review responses, review-request
automation, relist-churn-for-recency. **Permitted action surface (all
`listings_w`, already consented):** auto-renew toggle, reprice, SEO edit,
deactivate, gap-fill draft.

---

## 2. M8a scope — three things, default-off

1. **Chassis** — capability registry, tier engine, promotion ladder, governor
   (caps + spend + portfolio-% + kill-switch + proposal-TTL), a runner, the
   `action.*`/`capability.*`/`ops.*` event stream, two projections, and the
   `ops run|approve|reject|undo|halt|status` verbs. **Zero capabilities can
   execute until registered and gated.**
2. **One narrow objective** — "revenue per active listing over a window" → a
   ranked list of candidate actions. In M8a the only registered capability the
   objective can propose is **`listing.autorenew_off/_on`** (draft #5/#6,
   policy E1). The objective is a *ranker over the existing `dead_listings()`
   analytics*, not an open-ended optimizer.
3. **Feedback loop (R13)** — wire `ops/analytics.py` outputs into a
   `tuning.py` write path. **This has a safety fork — see §5 — and is the one
   place the kickoff and the draft disagree.** Needs a §7 decision.

**Excluded from M8a** (deferred to per-action specs, unchanged from draft §7):
reprice, SEO edit, deactivate, gap-fill, A/B, all customer contact (cut),
Meta (deferred), LLM brief narration, any daemon.

---

## 3. The chassis — extends built patterns, invents little

All under `src/shopsteward/pipeline/ops/` (no new import-linter contract; the
editing boundary is untouched). Module map follows draft §8.1. Concrete reuse:

| Chassis piece | Built precedent it extends |
|---|---|
| `live_autonomy_open()` / `_error()` gate | `live_gate.py` — same `SHOPSTEWARD_LIVE_AUTONOMY=1` + `ETSY_API_KEY` + `listings_w` scope shape as `live_etsy_write_open()` |
| `monthly_spend_cap_usd` enforcement | `llm_ledger.monthly_spend()` — pure sum over events; governor sums `action.executed.cost_usd` for the month identically |
| `ops.json` autonomy config block | `ops/config.py` `OpsConfig` + `seed`/`apply`/`get_ops_config` + `ops_config_hash` — add fields, no new machinery |
| `proj_actions`, `proj_capability_state` | `ops/projections.py` `rebuild_ops()` drop-and-rebuild — add two tables, no migration |
| `ops run/approve/...` CLI | `ops/cli.py` already wires `brief` + `config apply`; add verbs |
| Config-only-lowers-tier ceiling | `max_tier` in Python (draft §2.3 invariant 2) |

New files: `registry.py`, `tiers.py` (pure), `governor.py` (pure-ish),
`runner.py`, `capabilities/autorenew.py`, plus `models.py` additions
(`Tier`, `ProposedAction`, `ExecutionResult`, `CapabilitySpec`) and
`projections.py` additions. Events, idempotency (`action_id =
sha256(capability|target_id|inputs_hash|ops_config_hash|day)`), and the audit
chain are exactly draft §5/§8.3 — not restated here.

**Tier model** (draft §2, accepted or amended at §7.1): four tiers T0–T3;
`tier = max(reversibility, audience, money)`; `max_tier` ceiling in Python;
first live execution always T2; earned promotion, instant asymmetric demotion.
The chassis ships with **zero capabilities auto-approving** — `autonomy.enabled`
defaults `false`, so `ops run` no-ops until the operator opts in.

`ops.json` gains (draft §5 caps; values are §7.6):
```
"autonomy": {
  "enabled": false,
  "daily_action_cap": 10,
  "per_capability_daily_cap": 5,
  "weekly_catalog_pct_cap": 0.10,
  "monthly_spend_cap_usd": 0.00,        // §7 decision: 0.00 vs 20.00
  "proposal_ttl_days": 14,
  "ladder": { "promote_approvals": 20, "promote_min_days": 14,
              "t1_executions": 30, "t1_min_days": 30 }
}
```

---

## 4. The one narrow objective — revenue per active listing

Not an optimizer; a **deterministic ranker** binding the objective to the one
capability:

- Input: `proj_listing_daily` + `proj_sale_items` (both built).
- For each active/expired listing: revenue and views over the window (from
  analytics), and renewal exposure (a listing about to auto-renew that has
  earned $0 is *paying to stay dead*).
- Output: listings ranked by "renewal cost with no offsetting revenue," i.e.
  the existing `dead_listings()` set intersected with "auto-renew currently on
  and expiry near." Each becomes a `ProposedAction` for
  `listing.autorenew_off` carrying a one-sentence `reason` (draft §2.3
  invariant 4) and the `inputs_hash`.
- The capability's `propose()` reads only; `execute()` flips
  `should_auto_renew=false` via the Etsy write adapter (extended to carry the
  field); `undo()` flips it back (reversible for months before expiry).

This is the whole objective for M8a: it turns "revenue per active listing" into
"stop paying to renew listings that earn nothing," which is the safest possible
first money-relevant action (draft §3.1 row 5). Broader objectives (reprice for
demand, SEO for views) are **M7-gated** (draft §4) and out of scope.

---

## 5. The feedback loop (R13) — DECISION REQUIRED (kickoff ↔ draft conflict)

The kickoff lists "the analytics→tuning feedback loop" as part of minimum
viable autonomy. The draft (§4, §7, §14, §17.1 note on #36) says
**tuning-profile write-back is M7's deliverable** and, if done at all, is a
**T2 proposal** (#36) because a weight change *silently edits the operator's
creative queue* — it must never auto-write. Both can be honored, three ways:

- **Option A — Read-only loop (draft-faithful, smallest).** The loop is the
  *recommendation*, not the write: `ops/analytics.py` already produces
  `shoot_more` / trending. Surface them in the Brief (T0). **No `tuning.py`
  write in M8a**; the write-back waits for M7. Lowest risk; arguably doesn't
  fully satisfy "wire analytics→tuning."
- **Option B — T2 proposal capability (reconciles both).** Add a second
  capability `tuning.bump_subject_weight` (`max_tier=T2`, never higher): reads
  analytics, proposes a concrete `tuningprofile.updated` edit, executes only on
  one-tap approval, `undo()` restores the prior profile. Satisfies R13 *and*
  the "never silently edit the queue" rule. Cost: a second capability in M8a
  (draft ships exactly one).
- **Option C — Defer entirely to M7.** Treat R13 as M7 scope; M8a ships chassis
  + objective + auto-renew only.

**Recommendation: Option B**, because it is the honest reading of "feedback
loop" (it actually writes tuning) while staying inside the tier model's safety
(T2, undoable, one-tap). It adds one governed capability, which the chassis is
built to absorb. If you want M8a as small as possible, Option A/C.

---

## 6. R9 disposition

R9's premise ("active-only") is already false in code. Two honest options:
- **R9a (recommended): close R9 as already-resolved for M8a.** Active+expired
  is exactly what the auto-renew objective and `dead_listings()` need; draft,
  sold_out, inactive are not required by any M8a capability. Document it, no PR.
- **R9b: tiny widening** — add `sold_out`/`inactive` to `_LISTING_STATES` (one
  line) *if and only if* a later capability needs them. Not now (YAGNI).

Either way, the "R9 first PR" the handoff describes is not the prerequisite it
was written as. I recommend **R9a** + a note in the audit correcting the stale
finding.

---

## 7. Operator decisions (the approval gate — nothing builds until these land)

1. **Approve M8a as a v2 program + reconcile the PRD.** PRD §3.2 lists autonomy
   as a v1 non-goal (draft §0(c)/§17.3). Amend §3.2 / designate M8 as v2, or
   hold? *(Blocks everything.)*
2. **Accept the tier model** (§2: four tiers, `max(R,A,M)`, Python `max_tier`,
   earned ladder)? accept / amend / reject. *(Blocks the whole chassis.)*
3. **Feedback loop — Option A, B, or C** (§5)?
4. **Cap values** (§3 block), especially **`monthly_spend_cap_usd`: 0.00 or
   20.00?** (the draft contradicts itself). Confirm the rest or override.
5. **Scheduling:** Windows Task Scheduler calling `ops run` (draft §5, external
   kill-switch) or manual-only for M8a?
6. **R9:** confirm R9a (close as resolved) vs R9b (widen states now).

Downstream/no-rush (draft §17.7–17.16): customer-contact wall (confirmed by
policy — all cut), Meta naming/timing, extra Etsy scopes (none needed for M8a),
paid acquisition (cut), bookkeeping CSV, LLM narration. Not gating M8a.

---

## 8. Slice plan (each PR: subagent-TDD, governed, default-off)

| PR | Scope | Test that proves it |
|---|---|---|
| **1 ⭐ (mandatory §8.2 review)** | Chassis: `registry`/`tiers`/`governor`/`runner`, `action.*` events, `proj_actions`+`proj_capability_state`, caps/spend/portfolio/halt/TTL, `live_autonomy_open()`, `ops run --dry-run`. **Zero capabilities.** | cap/refusal/ladder/halt/idempotency against a stub capability |
| **2** | Capability `listing.autorenew_off/_on` + objective ranker + Etsy adapter `should_auto_renew` write + `FakeEtsyWriteAdapter` support + `undo()` | draft §11 E2E on fakes incl. undo byte-identical restore |
| **3** | `ops approve/reject/undo/halt/status` CLI + (optional) `/api/ops` + Brief autonomy/needs-you/refused sections | API/CLI tests; UI is the decision surface |
| **4** *(if §7.3 = Option B)* | `tuning.bump_subject_weight` T2 capability | proposal→approve→`tuningprofile.updated`; undo restores prior profile |
| **5** *(gated, operator smoke)* | One real listing: flip `should_auto_renew`, verify in Etsy UI, undo, verify restored; scrub fixture | — |

M8a ends at PR 5. M8b (deactivate, POD watchdog, Meta) and M8c (reprice/SEO,
M7-gated) stay deferred per draft §9.

---

*Nothing here is approved. Fastest path: answer §7.1 (approve + PRD), §7.2
(tier model), §7.3 (feedback-loop option). PR 1 is safe to start the moment
those three land.*
</content>
