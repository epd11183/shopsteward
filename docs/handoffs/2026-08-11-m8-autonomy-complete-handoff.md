# Handoff — M8 Autonomy (M8a chassis + M8b LLM shop-manager): BUILD COMPLETE

**Written:** 2026-08-11 (end of a long build session). **For:** the next session.
**One-line status:** the entire "Claude as shop owner/keeper/marketer" program is
**built, merged to `main`, CI-green (~940 tests), and default-OFF.** The only
remaining step is the **operator live-smoke**, which needs Eric's Etsy credentials
and cannot be done by the agent.

---

## Read first
- `docs/designs/2026-08-03-m8-autonomous-operations-draft.md` — the original tier
  model / arguments (§2 tiers, §5 events/caps, §13.2 the customer-contact
  arithmetic, §15 rejected alternatives).
- `docs/designs/2026-08-11-m8a-autonomy-spec.md` — the built M8a spec (chassis +
  one objective + feedback loop).
- `docs/designs/2026-08-11-m8b-llm-shop-manager.md` — the LLM-planner design
  (governed planner ≠ free-rein agent; the validation gate; the four hats).
- `docs/designs/2026-08-11-source-asset-head.md` — the archive/linkage that
  unblocked gap-fill reprints.
- `docs/policy/2026-08-11-autonomy-platform-policy.md` — Etsy/Meta policy verdicts
  (what's PERMITTED / PROHIBITED, with citations). **The cut list is load-bearing.**
- `CLAUDE.md` non-negotiables still apply.

---

## What is built and merged (all on `main`, all default-off)

**M8a — the deterministic chassis** (PRs #30–#33):
- `pipeline/ops/{registry,tiers,governor,runner,models,projections}.py` — capability
  registry + `max(R,A,M)` tier engine + earned/asymmetric ladder + governor
  (8-reason refusal precedence, spend cap, `weekly_catalog_pct_cap`, kill-switch,
  proposal TTL) + idempotent runner (propose→govern→execute) + `approve/reject/
  undo/halt/resume` + append-only `action.*`/`capability.*`/`ops.*` audit stream.
- `ops brief` (deterministic SQL Brief) + `ops run/approve/reject/undo/halt/resume/
  status` CLI.
- Feedback loop: `capabilities/tune_threshold.py` (T2 proposal that fits an inert
  `dead_listing.min_observed_days` to the data).

**M8b — the LLM shop-manager** (PRs #35, #36, #37, #38, #39, #41, #42, #43):
- `adapters/planner/{interface,openrouter,fake}.py` — the LLM adapter (OpenRouter,
  fake default), `narrate()` + `plan()`.
- `pipeline/ops/planner.py` — `narrate_brief` (LLM narration over the deterministic
  Brief, cost-capped via `llm_ledger`) + `plan_proposals` = **the validation gate**:
  every LLM `ProposalIntent` must survive `customer_contact_barred → unknown_capability
  → policy_unverified → ungrounded (materialize None)` before it can become an
  `action.proposed`; each drop is logged `planner.intent_dropped`. `_build_facts_json`
  feeds the LLM only real SQL figures (dead/trending/viewed_not_sold/top_sellers).
- **Seven governed capabilities** (`pipeline/ops/capabilities/`), all propose-only
  (ship T2), each with `materialize()` sharing `propose()`'s grounding so the LLM
  can only pick real, blessed targets:
  - `autorenew.py` — `listing.autorenew_off` (T1 ceiling; dead+active+auto-renew-on).
  - `tune_threshold.py` — `ops.tune_threshold` (T2; config feedback loop).
  - `reprice.py` — `listing.reprice` (T2, never promotable; **DIGITAL-ONLY** via the
    authoritative `listingdraft.provider_linked` signal + conservative title check;
    price bounds drop-not-clamp incl. NaN/inf; reversible).
  - `seo_edit.py` — `listing.seo_edit` (T2; title+tags only, POD-ok; **description
    deferred** — no baseline in the sync model; reversible).
  - `deactivate.py` — `listing.deactivate` (T1 ceiling; state-only via the new
    `update_listing_state`; the capability the `weekly_catalog_pct_cap` protects).
  - `gapfill.py` — `listing.gapfill_reprint` (T2; reprint a proven best-seller in a
    new POD format; **zero live surface in execute()** — offline `build_pod_reprint`,
    flows to Gate 3 via the operator's shop-build; `undo=None`, Gate 3 is the reversal).
  - `caption_draft.py` — `social.caption_draft` (T2; **assist-only**, writes an IG/FB
    caption surfaced in the Brief for MANUAL posting; **publishes nothing**).
- **Source-asset head** (PRs #40, #41): `pipeline/listings/{archive,asset_store_config,
  source_assets}.py` + `proj_asset_store` + `build_pod_reprint`. Durable local master
  archive `data/asset_store/{photo_id}/{sha256}` (verbatim copy, no AI touch),
  sha256-verified retrieval fallback, `resolve_source(listing_id)`. **Degrade-and-
  continue** (an archive disk error never breaks a build).

**Also:** PRD §3.2 amended (autonomy → approved v2/M8a); audit R9 corrected
(active+expired was already fixed); **CI green-up** (#34: repo `ruff format` +
`.gitleaks.toml`).

---

## THE ONLY REMAINING WORK — the operator live-smoke (Eric-run)
Nothing has ever run live. Everything is default-off. To turn M8b on for the real
shop (do this incrementally — narrate first, then propose, then approve one):
1. `config/defaults/ops.json`: `autonomy.enabled=true`, `autonomy.planner_enabled=true`
   (and consider a `monthly_spend_cap_usd` > 0 only if you want auto-renew-ON/renewals;
   auto-renew-OFF and everything else run at $0.00). Then `ops config apply`.
2. LLM: `SHOPSTEWARD_LIVE_PLANNER=1` + `OPENROUTER_API_KEY` → try `ops brief --narrate`
   first (read-only, cheap) to sanity-check narration before enabling proposals.
3. Live writes: `SHOPSTEWARD_LIVE_AUTONOMY=1` + `ETSY_API_KEY` + `shopsteward etsy auth`
   with a `listings_w` token. Run `ops run --live-autonomy`.
4. `ops brief` → the `NEEDS YOU` section shows proposals with copy-pasteable
   `action_id`s → `ops approve <id>` (or `reject`). Everything is T2 — you approve
   each; capabilities earn autonomy only via the ladder (20 approvals + 14 days → T1).
5. `ops halt` is the in-band kill switch; disabling the scheduled task (if you set one
   up via Windows Task Scheduler → `ops run`) is the belt-and-braces one.

---

## Landmines / things NOT to redo (verified this session)
- **The Meta/IG publish path is deliberately unwired** — blocked on Meta App Review +
  Business Verification (policy doc). `caption_draft` only DRAFTS text; do not wire
  `adapters/meta` without clearing App Review first.
- **reprice is digital-only ON PURPOSE** (POD price edits rewrite provider SKUs — draft
  #9b). The guard is the authoritative `listingdraft.provider_linked` signal + a
  conservative title check. Don't loosen it.
- **`description` SEO editing is deferred** — the sync model (`EtsyListing`,
  `proj_listings`) captures title/tags but NOT description, so there's no baseline to
  diff/undo. Add description to the sync model first if you want it.
- **gap-fill only reprints PIPELINE-CREATED, ARCHIVED best-sellers** — a manual/
  pre-pipeline listing has no `proj_listing_drafts` linkage → `resolve_source` returns
  None → not reprintable (honest ceiling, surfaced).
- **The reviewer caught 9 real bugs green tests missed** (NaN-price, ladder-fold,
  double-execute, SEO/planner facts gaps, archive-breaks-build, ops-undo crash,
  delisted-item caption). **Keep the adversarial review step for every new slice.**
- **Never `ruff format .` repo-wide in a scoped PR** — CI enforces `ruff format --check
  .` and the repo is now clean; format only files you edit
  ([memory](shopsteward-impl-subagent-ruff-format-repowide.md)).
- **`data/` is never committed/printed** — the asset store lives there; tests point the
  store root at a tmp dir.

---

## Nice-to-haves / small follow-ups (none blocking)
- Update PRD §10 milestone table + CLAUDE.md "Current focus" to record M8a/M8b shipped.
- LLM-narrated Brief and the caption/gap-fill facts could be tuned once you see real
  proposals.
- A `# ponytail:` note in `archive.py` flags the O(n) idempotency scan (add an index
  if the archive grows).
- Deferred M8b niceties: description-SEO (needs sync change), a full in-`execute()`
  gap-fill→Gate 3 orchestration (currently the reprint reaches Gate 3 via the
  operator's shop-build), and Meta publish (App Review).

---

## Key commands
- `uv run pytest` — full suite (~940 green).
- `shopsteward ops brief [--narrate]` / `ops run [--live-autonomy]` /
  `ops approve|reject|undo|halt|resume|status`.
- `shopsteward shop build <folder>` — the operator's listing-creation pipeline (POD
  reprints from gap-fill flow through this to Gate 3).

*Bottom line: the build is done and safe-by-default. The next move is Eric turning it
on, one approved proposal at a time.*
</content>
