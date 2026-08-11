# Design: M8b — The LLM Shop-Manager (a governed planner on the M8a autonomy chassis)

**Status:** PROPOSED, awaiting operator approval. **No code until approved.**
**Date:** 2026-08-11. **Author:** architect subagent + orchestrator synthesis.
**Amends:** M8 draft §15 (adds the governed-planner distinction), PRD §10 (new
milestone), CLAUDE.md "Current focus."
**Depends on (all merged on `main`):** `pipeline/ops/{registry,tiers,governor,runner,models,projections,brief,analytics,cli}.py`, `capabilities/{autorenew,tune_threshold}.py`, `adapters/copy/`, `pipeline/llm_ledger.py`, `docs/policy/2026-08-11-autonomy-platform-policy.md`.

---

## 0. The one-paragraph thesis

The operator wants Claude to be shop owner/keeper/marketer/support while he stays
the artist. The M8a chassis already gives us a finite set of governed, reversible
capabilities and an earned-autonomy ladder. **The only thing missing is judgment
about *which* capabilities fire, on *which* targets, with *what* params — today
that judgment is a hardcoded heuristic in each capability's `propose()`.** M8b
inserts an LLM *planner* that supplies that judgment. Crucially, **the LLM never
executes and never invents an action.** It emits a structured list of proposal
*intents* drawn from the registered capability set; each intent is validated
against the registry and *re-materialized from real SQL* by the capability itself
before it can become an `action.proposed`. Anything malformed, unknown,
`policy_verified=false`, or ungrounded is dropped *before* it enters the existing
propose→govern→execute path. The action space stays finite; every consequence
stays reversible and gated. This is precisely why a governed planner is safe where
§15's free-rein agent was not.

---

## 1. Placement — where the planner sits

New module `src/shopsteward/pipeline/ops/planner.py` and a new adapter package
`src/shopsteward/adapters/planner/` (`interface.py`, `openrouter.py`, `fake.py`).
Nothing else moves.

```
analytics.py (real SQL facts) ─┐
registry.REGISTRY (catalog)    ├─► planner.plan()
proj_capability_state (ladder) ┘        │
                                        ▼  emits ProposalIntent[]  (LLM, behind adapter)
                              planner validation gate (pure, no LLM)
                                        │  materialize + drop invalid
                                        ▼  ProposedAction[]  (identical shape to today)
                              runner.run()  ── EXISTING propose → govern → execute
```

- **Reads only:** the deterministic `Brief` facts, the capability *catalog* (key,
  one-line purpose, param schema, `max_tier`, `policy_verified` — a static
  description, never live SDK access), and current ladder/caps/spend state.
- **Emits:** a validated `list[ProposedAction]` fed to the **existing**
  `runner.run()` (or, for Gate-3 artifacts, the existing copy/mockup/Gate-3 draft
  path). **No new execution path, no new execution event type.** The planner is a
  *smarter proposer*; from `action.proposed` onward every byte flows through code
  that already shipped and is already tested.
- The runner needs one change only: today capabilities are hardcoded in `cli.py`;
  M8b lets the planner supply the proposal list. The governor, tier engine,
  ladder, idempotency, and undo are untouched.

**Rejected placements:** (a) *LLM inside the runner loop* — couples the untested
planner to the execute path; keep it strictly upstream of `action.proposed`.
(b) *A new `pipeline/agent/` package* — signals "agent with reins"; this is an ops
proposer, it lives in `ops/`. (c) *A `PlannerAdapter` that returns executable
calls* — that is the §15 free-rein design; the adapter returns *intents*, never calls.

---

## 2. The structured-proposal contract + grounding (the safety core)

The LLM's entire output is a strict-schema JSON array of **intents** — never a
`ProposedAction`, never an `action_id`, never an `inputs_hash`, never a raw number
the runner trusts:

```python
class ProposalIntent(BaseModel):        # adapters/planner/interface.py
    capability_key: str                 # MUST match a REGISTRY key
    target_id: str                      # e.g. a listing_id as str
    params: dict[str, str | int | float | bool] = {}   # capability-specific, schema-validated
    reason: str = Field(min_length=1)   # one human sentence, must cite real figures
```

Emitted via OpenRouter `response_format: json_schema, strict: true` (identical
mechanism to `adapters/copy/openrouter.py`). The **validation gate in `planner.py`
is pure and LLM-free** and runs every intent through, dropping (not raising, not
repairing) any that fail — each drop is recorded as a `planner.intent_dropped`
event with the reason, so "why didn't Claude propose X" is answerable from the log
(mirrors the governor's refusal-is-an-event precedent):

1. **Structural** — Pydantic parse; a malformed item is dropped, a wholly-
   unparseable response yields zero proposals.
2. **Registry** — `capability_key in registry.REGISTRY` else drop
   (`unknown_capability`). This is the finite-action-space guarantee.
3. **Policy** — `cap.policy_verified` else drop (`policy_unverified`). A prohibited
   capability (buyer messaging, refunds, coupons, Etsy Ads) is never registered
   *or* is registered with `policy_verified=False`, so it can never survive this
   gate — belt and suspenders with the governor's own `POLICY_UNVERIFIED` refusal.
4. **Materialize + re-ground (anti-hallucination)** — call the capability's new
   `materialize(conn, user_id, cfg, intent) -> ProposedAction | None`. This method
   **re-derives the load-bearing numbers from real SQL**, computes the canonical
   `inputs_hash`/`action_id` via `registry.compute_action_id`, validates `params`
   against the target's real state, and returns `None` (drop, `ungrounded`) if the
   target doesn't exist, the params are out of the capability's allowed range, or
   there is no real change to make. **The LLM's `reason` prose is carried through
   only as commentary; the figures the governor and Brief rely on come from SQL,
   not from the model.** A hallucinated listing_id, a fabricated "views" count, or
   an out-of-band price all die here.
5. **Invariants carry over unchanged** — one-sentence `reason`, `inputs_hash`
   computed by the capability, effective tier stamped by the runner. Idempotency by
   `action_id` is automatic because `materialize` uses the same `compute_action_id`.

The planner is **fed only real SQL figures** and instructed to ground each `reason`
in them; but grounding is *enforced structurally* by step 4, not by trusting the
prompt. This is the airtight line: the model chooses among finite, pre-validated,
self-reversing options; it cannot widen the action space or smuggle in an unbacked
number.

---

## 3. Propose-only-first + the ladder (unchanged autonomy semantics)

The LLM adds judgment to **which** capabilities fire, on **which** targets, with
**what** params. It does **not** touch tiers, the governor, or the caps. Every
planner-originated proposal enters at the capability's current **effective tier** —
T2/PROPOSE for every new capability — and lands in the Brief's `NEEDS YOU` for
operator approval. Autonomy is earned exactly as today: approvals + clock → T1;
notified executions + clock → T0; one rejection or undo demotes and resets,
asymmetrically, immediately.

**The planner cannot change a tier, cannot bypass `govern()`, and cannot
self-promote.** A planner-originated `action.approved{by:"operator"}` bumps the
ladder identically to a deterministic one — the ladder measures *the operator's
trust in the capability*, and it does not care whether a heuristic or the LLM chose
the target. `max_tier` stays a Python attribute (config can only restrict), so no
prompt and no config edit can make buyer-messaging or any T3 capability autonomous.

---

## 4. The four hats, honestly

| Hat | What's actually buildable | Mechanism | Tier |
|---|---|---|---|
| **Keeper** | auto-renew off/on (built), **reprice** (E2), **SEO edit** (E3), **deactivate/pause** (E4) | new governed capabilities under `listings_w` (no new Etsy scope), each with a real `undo()` | T2 → earn per capability |
| **Marketer** | listing copy/SEO (E3) + **IG/FB post drafts** | copy via existing `adapters/copy`; social drafts → **Gate 3**. IG/FB *publishing* PERMITTED but gated on Meta App Review + Business Verification — adapter parked until it clears | drafts = Gate 3; live SEO edits T2 |
| **Owner** | strategy: shoot-more, pricing posture, **gap-fill drafts** (E5) | recommendations surface in the (narrated) Brief; gap-fill ends in an unpublished `createDraftListing` → Gate 3 | recommendations read-only; drafts Gate 3 |
| **Support** | **assist-only.** Bulleted context for the operator; **NEVER machine-sent. Out of v1.** | Brief may *surface* a note; no send path exists in code | — |

**The support wall is a hard line, on evidence** (policy doc): buyer messaging (E11)
PROHIBITED + barred by API ToU §5(15)–(16); review responses (E13) no write API;
refunds/disputes (E14) no API. And M8 draft §13.2: the expected value of automated
customer contact is *negative and not close* — one bad message is a permanent
public artefact on a small-n rating. §13.2 also warns the *assist* path launders
the risk back in ("a fluent ready-to-send draft will eventually be sent unread").
**So v1 support is bulleted context only, and out of the first slices. No
`policy_verified=True` support capability is ever registered.**

---

## 5. LLM-narrated Brief

`ops brief --narrate` passes the *already-computed deterministic Brief facts* to
`PlannerAdapter.narrate(facts) -> str`. Claude explains its reasoning in plain
language over the real `GROUP BY` numbers. It **must cite the real figures** it was
handed and invents no data (the deterministic Brief remains the source of truth and
is printed alongside; narration is commentary, never a substitute). Default-off,
gated, cost-capped (§6). Smallest first slice: zero new actions, pure read.

*This deliberately reverses M8 draft §15's "no LLM brief in v1."* That rejection was
correct when the Brief was thin and unvalidated. It is now a **narration layer over
a mature deterministic Brief**, opt-in and capped — the facts are computed and
asserted deterministically; only the prose is LLM. A conscious amendment, flagged.

---

## 6. Cost + testability

- **Transport:** reuse the `adapters/copy/openrouter.py` OpenRouter/httpx pattern
  (strict `json_schema`, no vendor SDK). **No new provider, no new dependency.**
  Same triple-gate (flag + env + key) as copy/vision.
- **Cost cap:** the planner emits `llm.call` events with `est_cost_usd`, sharing the
  **existing `pipeline/llm_ledger.py` monthly cap** — over cap → skip, fall back to
  the deterministic engine. *(§12 decision: reuse the shared $10 cap vs. a sibling
  planner cap — recommend reuse.)* The autonomy `monthly_spend_cap_usd` continues to
  bound *action execution* cost separately.
- **Prompt-size cap (CFO improvement, accepted):** feed the planner only the top-N
  Brief rows already surfaced (dead/trending/viewed-not-sold), not the full catalog;
  log per-run token counts so cost-per-proposal is visible before any capability
  earns autonomy.
- **Call budget:** one `plan()` (and at most one `narrate()`) per scheduled
  `ops run` — cheap model routine, escalate only when the cheap model returns zero
  valid intents on a run with live signals (a config knob).
- **Offline testability (hard guardrail):** the LLM is behind `PlannerAdapter` with a
  `FakePlannerAdapter` returning canned `ProposalIntent[]` (the `FakeCopyAdapter`
  precedent). **Every governor/runner/ladder test stays deterministic; no test hits a
  live model.**

**Smallest test that proves the safety thesis** — *"an LLM proposal for an unknown/
prohibited capability is rejected before execution"*:

```
FakePlannerAdapter returns [
  {capability_key: "listing.send_buyer_message", target_id: "42",     params: {}, reason: "reply to buyer"},
  {capability_key: "listing.autorenew_off",       target_id: "999999", params: {}, reason: "invented listing"},
]
planner.plan() → assert:
  - zero action.proposed events written
  - govern() never called
  - planner.intent_dropped{reason:"unknown_capability"} and {reason:"ungrounded"} logged
```

First item dropped at registry check; second (real capability, hallucinated target)
dropped at materialize (listing 999999 has no `etsy.listing.observed` row). Neither
reaches the governor.

---

## 7. Guardrails / rollback

- **Master flag default-off:** `cfg.autonomy.planner_enabled: bool = False`
  (config-over-code, seeded from `ops.json`). False → `ops run` uses today's
  deterministic `propose()` path unchanged.
- **Kill-switch + caps apply identically:** `ops halt` refuses *every* action
  regardless of origin (`HALTED` top precedence). Daily/per-capability/portfolio/
  budget caps act on `action.executed`, which is origin-blind.
- **Cost cap bounds spend** (planner/narrate via `llm_ledger`; execution via the
  autonomy budget cap).
- **Rollback = flip the flag.** Falls back to the deterministic engine, still tested.
  No schema to unwind (new events additive/append-only).

---

## 8. Slice plan (each default-off, one governed slice per PR, subagent-TDD)

1. **Narrated Brief only — zero new actions.** `PlannerAdapter.narrate()` +
   `FakePlannerAdapter` + `ops brief --narrate`. Proves transport, ledger gate,
   "cites real numbers, invents none." Lowest risk, immediate value.
2. **Planner emitting intents for the *existing* capabilities.** `planner.plan()` +
   validation gate + `materialize()` on `autorenew_off`/`tune_threshold`. Proves the
   safety gate (§6 rejection test), no new action surface.
3. **Keeper: reprice (E2).** First new per-action capability with a real `undo()`
   (restore prior price). T2, `listings_w`, honest-pricing (§1.d.6) enforced in
   `materialize`.
4. **Keeper: SEO edit (E3) + deactivate (E4).** Each T2, self-reversing.
5. **Owner: gap-fill drafts (E5) → Gate 3.** Ends in an unpublished draft.
6. **Marketer: IG/FB post drafts → Gate 3.** Drafts only; publishing deferred until
   Meta App Review + Business Verification clears, then the parked `adapters/meta` is
   wired. **(CMO improvement, accepted:** ship draft-*generation* decoupled from the
   publish gate so the marketing brain lands now, auto-publish later.)

Support is **not** on the roadmap as a machine-send capability.

---

## 9. PRD / CLAUDE.md deltas

- **M8 draft §15 amendment (add):** *"Rejected: an LLM agent with tool access and
  free rein — but NOT a governed planner. The distinction is load-bearing. A
  free-rein agent has an unbounded action space, cannot be tested offline, and has
  no guaranteed `undo()`. The M8b planner emits only validated intents drawn from
  the finite capability registry; each is re-grounded from SQL and materialized into
  a self-reversing `ProposedAction` before it can be proposed, and every consequence
  still flows through the governor, caps, ladder, and undo."*
- **PRD §10:** new milestone **M8b — LLM shop-manager (governed planner)**, v2,
  propose-only-first, sliced per §8; supersede the §15 "no LLM brief in v1" note with
  the narration reversal (§5).
- **CLAUDE.md "Current focus":** M8b as the layer on the merged M8a chassis;
  reaffirm "AI never touches the photograph" and "no machine-sent customer contact."
- **Config-over-code:** `planner_enabled` (default false), planner model IDs +
  pricing (mirrors copy pricing config), escalation knob — all in `ops.json`.
  `max_tier`/`policy_verified` stay Python.
- **New adapter interface** (operator review): `adapters/planner/interface.py`. New
  capabilities each require operator review as added.

---

## 10. C-Suite critique (PRD §8.3) — improvements accepted into the design above

**CTO.** Strength: zero new execution path — the planner is strictly upstream of
`action.proposed`, so the tested chassis is untouched and the fake keeps the suite
deterministic. Concern: `materialize()` becoming a second, subtly-divergent copy of
`propose()`. *Improvement (accepted, §2):* `propose()` and `materialize()` share one
private grounding function — one code path, two entry points — so they can't disagree
(the `tune_threshold` recompute-so-they-can't-disagree pattern generalized).

**CFO.** Two independent caps (thinking vs acting) is right; reusing the ledger
avoids a second accounting surface. Worry: unbounded planner spend as the catalog
grows. *Improvement (accepted, §6):* cap the prompt to the top-N Brief rows and log
per-run token counts; a capability that costs more to plan than it saves must not
climb the ladder.

**CMO.** Routing IG/FB through Gate-3 drafts is correct, but gating *all* social
behind Meta App Review under-serves marketing. *Improvement (accepted, §8 slice 6):*
ship draft-*generation* now (Claude writes caption + selects mockup into a Gate-3
draft the operator posts manually), decoupled from the API-publish gate.

**CPO.** Propose-only-first with earned autonomy is the arc the operator asked for.
Risk: Brief overload recreating the "unbounded stream of interruptions" §15 rejected.
*Improvement (accepted, §12.8):* the planner ranks and caps its own output — one
highest-value action per capability per run by default, config-raisable — so
`NEEDS YOU` stays short and high-signal.

**Chief Legal.** Defensible because built *on* the policy verdicts: the planner can
only name `policy_verified=True` capabilities, and the prohibited set is structurally
unregisterable, so no prompt can reach it. The support wall must stay absolute
(§13.2 arithmetic; the assist path launders risk through a human). *Improvement
(accepted, §2):* add a `customer_contact_barred` drop-reason to the validation gate
so any customer-addressed intent dies even if a future capability is mis-registered —
the ToU §5 wall enforced twice (registry + gate) and provable from the log. Also
surface the standing Etsy obligations (Application-Purpose approval; 6-month
dormant-app rule) in the Brief — operator registration steps, not code.

---

## 11. Load-bearing assumptions (flag before build)

1. **Value-add is judgment, not new mechanism.** Route a capability through the LLM
   only where a deterministic heuristic is too blunt (reprice amount, which SEO edit,
   which dead listing). If `propose()` is already good enough, don't add the LLM there
   (YAGNI).
2. **`materialize()` can always re-ground from SQL.** Every planner-driven capability
   must have a deterministic, SQL-derivable notion of a valid target + params.
3. **Shared `llm_ledger` cap is acceptable** for planner + copy + vision (else a
   sibling cap — §12.3).
4. **Meta App Review timeline is external/unbounded;** auto-publish is scheduled after
   it clears; draft-generation ships independently.
5. **Single-operator, single-tenant** today; `user_id` on every new event/table.

---

## 12. Operator decisions this design needs

1. **Approve the governed-planner distinction + the §15 amendment** (§9) — the core
   safety framing.
2. **Approve the new `adapters/planner/` interface + the `ProposalIntent` contract** (§1–2).
3. **Cost cap:** reuse the shared $10 `llm_ledger` cap, or a dedicated planner cap?
   (Recommend reuse.)
4. **Model routing:** cheap model for routine `plan()`/`narrate()`, stronger for
   escalation (AI-provider selection needs operator review per CLAUDE.md). Recommend
   the existing OpenRouter transport + the tuning profile's cheap model.
5. **Confirm the §5 narration reversal** (opt-in narration over a mature deterministic
   Brief).
6. **Confirm the support line:** bulleted context only, no send path, out of the first
   slices — permanently, per §13.2.
7. **Slice-1 scope:** narrated Brief first (recommended), or straight to
   planner-drives-existing-capabilities?
8. **Per-run proposal cap** (CPO): default one highest-value action per capability per
   run?

---

*Save target: this file. Nothing above is approved; no code until §12.1/12.2/12.7 land.*
</content>
