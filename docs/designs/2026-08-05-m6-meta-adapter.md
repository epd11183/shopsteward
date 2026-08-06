# M6 Design — Instagram + Facebook Scheduled Posting (Meta Adapter)

**Architect output 2026-08-05 · Status: READY FOR OPERATOR REVIEW (PRD §8.2)**

**OPERATOR REVIEW REQUIRED** — This is the first PR of milestone M6 and introduces a new external service (Meta Graph API). Per CLAUDE.md §8.2 and PRD §8.2, operator approval is required before any code is written.

## Context: Why Now

Eric's Instagram Business/Creator account + linked Facebook Page account drive **75% of historical sales** — far exceeding organic Etsy traffic. Current posting cadence has dropped ("I haven't posted much lately"), correlating with flat recent sales. PRD §3.1 already mandates this work: "Enforce an Instagram cadence of at least 4 posts per week with no more than one operator tap per post." PRD §1.1 already authorizes AI for "Instagram caption + hashtag generation." This is overdue scope, not new scope.

## 1. Proposal: Three-Part Design

### 1.1 Module: `src/shopsteward/adapters/meta/` (Single Adapter)

Build ONE adapter for the Meta Graph API, serving both IG Business and linked FB Page. Two public interfaces:

- **`IgPublisher`**: schedule/publish/unschedule posts to Instagram Business account
- **`PagePublisher`**: same interface, for linked Facebook Page

**Why NOT separate `adapters/instagram/` and `adapters/facebook/`?** The Graph API is one—one OAuth token, one rate limit, one app. Splitting them creates false coupling (shared token refresh, coordinated scheduling across modules). This forces a rename later (M8c Facebook ads, future Reels). Name it for what it is: the Meta Graph client.

**Architectural decision flagged:** CLAUDE.md line 29 and PRD §7.1 currently say `adapters/instagram`. M8 draft §8.2 recommends `adapters/meta/`. This design settles it **now**—renaming later costs import changes + import-linter amendment + §8.2 re-review. Naming correctly now costs nothing. If approved, amend CLAUDE.md and PRD §7.1.

### 1.2 Posting Is Execution, Not Creation — No Fourth Gate

**Gate 3 approval is the creative gate.** Posting does NOT add a fourth touchpoint:

- Gate 3 UI: Operator reviews the draft listing + IG asset (photo + caption). Tap "approve" → listing publishes to Etsy AND IG post queues for scheduling.
- M6 runner (`shopsteward meta schedule`): Invoked on-demand or by OS scheduler (Windows Task Scheduler, cron). Reads approval queue, batches posts to meet 4x/week cadence, executes via Graph API.
- M8 Brief: Surfaces scheduled posts overnight; operator can undo within 7 days (reverts schedule entry, unsends API call).

This satisfies all three constraints:
- No fourth creative gate (CLAUDE.md)
- Cadence enforceable (queue-based scheduling)
- Undo works (M8 tier T1 for FB, T0 for IG—post execution is already approved)

### 1.3 Caption + Hashtag Generation: Reuse Copy Adapter

Do NOT create a separate `IgCopyAdapter`. Instead:

- Extend `CopyInputs`/`CopyVerdict` to carry `intent: "etsy_listing" | "ig_caption"`
- House style guide (`config/defaults/house_style.json`) includes an `ig_caption` template (5–10 hashtags, emoji once, narrative second)
- `CopyAdapter.generate_copy()` takes the intent; prompt template selected from house style guide
- Configuration `caption_generation.mode` selects "gate3_or_manual": generate at Gate 3 (alongside listing copy), or lazily at schedule time if operator leaves caption blank

**Why:** Avoids adapter duplication. All LLM routing stays under the existing $10/month soft cap (decision 36, decision 38). Vision → Copy → IG captions use one OpenRouter transport with one provider (anthropic/claude-sonnet-5 default, configurable).

---

## 2. Rejected Alternatives

| Rejected Idea | Why Rejected |
|---|---|
| Two adapters: `adapters/instagram/` + `adapters/facebook/` | Forces duplicate auth logic, shared token-refresh coordination, scheduling across modules. Graph API is one; pretending in architecture is debt. Later M8c ads and Reels will expose the lie. |
| Use third-party scheduler (Buffer, Later, Hootsuite) as adapter | Brief (M8) is the audit log. Third-party means IG state in two places; undo is not 1-1 reversible. Violates local-first principle. CLAUDE.md's append-only event log becomes fiction. |
| Generate captions at schedule time, require re-approval | That is a fourth gate, forbidden. Captions are copy; copy is approved at Gate 3. Scheduling is execution, not creation. Violates the CLAUDE.md "no fourth gate" rule. |
| Post immediately on Gate 3 approval, no scheduling | Cadence enforcement impossible. Operator can't guarantee 4x/week if system posts at random times. Requires a queue + scheduler + cadence logic. |

---

## 3. Guardrail Impact Summary

| Guardrail | Status | Impact |
|---|---|---|
| **Three gates** | Preserved | Posting is execution of a Gate 3 decision. No new creative touchpoint. M8 tier table row #24 = T0 (no new gate). |
| **Monolithic core / pluggable adapters** | Met | Core imports only `adapters/meta/interface.py`, never the Meta SDK. Posting logic lives in core (`pipeline/meta/runner.py`); adapter handles only Graph API calls. |
| **Editing-module boundary** | Untouched | `editing/` imports nothing from `pipeline/meta/` or `adapters/meta/`. No import-linter amendment needed. |
| **Landing-folder handoff** | Not involved | IG assets flow through Gate 3 hero path, not the landing folder. |
| **Event-sourced SQLite** | Met | `hero.approved` carries IG asset metadata. `meta.post.proposed`, `.scheduled`, `.published`, `.undone` append immutably. Undo appends `meta.post.undone`, never deletes. |
| **Configuration over code** | Met | Cadence target (4x/week), hashtag strategy, caption mode, FB on/off toggle, retry limits, provider routing all in `config/defaults/meta.json`. No hardcoding. |
| **AI never touches the photograph** | Met | Image selected at Gate 3; M6 only queues it for posting. Caption + hashtags are text only; mockups never regenerated. |
| **user_id on every major table** | Met | Projections `proj_meta_queue`, `proj_meta_posts` carry `user_id`. Multi-tenant readiness preserved. |
| **No live external APIs in tests** | Met | All tests run on `FakeMeta` adapter with recorded, scrubbed fixture responses (successful post, rate-limit error, token-expiry scenario). Slice 5 is sole live smoke test, separately gated. |

---

## 4. Module Map

```
src/shopsteward/adapters/meta/
  __init__.py              Re-exports public API
  interface.py             IgPublisher, PagePublisher protocols + MetaPost model
  auth.py                  MetaAuthStore (token storage, refresh logic)
  live.py                  LiveMeta (Graph API implementation, httpx only)
  fake.py                  FakeMeta (complete mock for all tests)

config/defaults/meta.json  Cadence, caption mode, FB toggle, hashtag strategy,
                           posting rules (seeded at first run, stored in DB)

src/shopsteward/pipeline/meta/
  __init__.py
  models.py                MetaPost, ScheduledPost event models
  projections.py           proj_meta_queue, proj_meta_posts (drop-and-rebuild)
  runner.py                Orchestrator: propose -> govern -> schedule
  api.py                   GET /api/meta/queue, PATCH /api/meta/{post_id}/undo
  cli.py                   shopsteward meta schedule|undo|auth commands

tests/adapters/meta/
  test_interface.py        FakeMeta correctness, token storage behavior

tests/pipeline/meta/
  test_e2e_cadence.py      Full flow: approve -> schedule -> cadence -> undo
```

**No new import-linter contract.** `meta` lives inside `pipeline/`, same as M5b precedent (POD). Editing boundary untouched.

---

## 5. Live-Write Triple Gate (Follows Etsy M5a Pattern Exactly)

Following `src/shopsteward/pipeline/live_gate.py` precedent:

```python
def live_meta_write_open() -> bool:
    """True iff SHOPSTEWARD_LIVE_META_WRITE=1, META_API_KEY set,
    and tokens on disk with instagram_basic_publish scope."""
    if os.environ.get("SHOPSTEWARD_LIVE_META_WRITE") != "1":
        return False
    if not os.environ.get("META_API_KEY"):
        return False
    tokens = MetaAuthStore().load()
    return tokens is not None and "instagram_basic_publish" in tokens.scopes

def live_meta_write_error() -> str:
    return (
        "Live Instagram posting is gated on operator approval (PRD §8.4): set "
        "SHOPSTEWARD_LIVE_META_WRITE=1 and META_API_KEY, run `shopsteward meta "
        "auth` with instagram_basic_publish scope, then re-run with --live-meta-write."
    )
```

**Environment variables:**
- `SHOPSTEWARD_LIVE_META_WRITE=1` (production mode gate)
- `META_API_KEY` (OAuth client ID, required if live)

**Tokens stored:** `data/meta_tokens.json` (gitignored, auto-refresh + rotate on use following Meta's token lifecycle).

**Auth entry point:** `shopsteward meta auth` (localhost OAuth redirect, same pattern as Etsy).

---

## 6. Configuration Schema: `config/defaults/meta.json`

```json
{
  "schema": "shopsteward.meta/1",
  "cadence": {
    "target_posts_per_week": 4,
    "distribution": "even",
    "preferred_hours_utc": [10, 14, 18, 22],
    "timezone": "America/Chicago"
  },
  "instagram": {
    "enabled": true,
    "account_type": "business",
    "retry_on_failure": true,
    "max_retries": 3,
    "retry_backoff_minutes": [5, 15, 60]
  },
  "facebook": {
    "enabled": true,
    "page_id": "auto_discovered_on_auth",
    "share_same_asset": true
  },
  "caption_generation": {
    "mode": "gate3_or_manual",
    "provider": "openrouter",
    "model": "anthropic/claude-sonnet-5",
    "template_key": "ig_caption"
  },
  "hashtag_strategy": {
    "count_min": 5,
    "count_max": 10,
    "branded": ["#photographybyericd"],
    "trending_refresh_days": 7
  },
  "posting_rules": {
    "max_queue_size": 52,
    "max_scheduled_days_ahead": 30,
    "cooldown_between_posts_hours": 6,
    "prevent_same_image_posts_days": 30
  }
}
```

Config sourcing: seed on first run, stored in SQLite `config` table with `config_hash` (M5a precedent).

---

## 7. Meta Policy Questions — RESOLVED 2026-08-05

Answered from Meta's official developer docs (developers.facebook.com), operator-directed research pass. Recorded here per the process this design specified; PRD §13 decision amendment still to be filed.

| Q | Question | Answer | Source |
|---|---|---|---|
| **M1** | Creator vs Business account parity | **Identical.** Same endpoints, params, permissions, rate limits — both are "professional accounts" to the API. | `developers.facebook.com/docs/instagram-platform/overview/` |
| **M2** | One token for linked IG Business + FB Page, or separate? | **One, via the Facebook Login path.** Instagram Login (newer, IG-only) uses an Instagram User token and never touches the Page. Facebook Login uses a **Page Access Token** that covers both IG publishing (`instagram_content_publish`) and Page publishing (`pages_manage_posts`) — this is the flow to build against, since M6 wants both surfaces under one adapter. | `docs/instagram-platform/instagram-api-with-instagram-login/`, `docs/instagram-platform/create-an-instagram-app/` |
| **M3** | Publishing rate limit | **100 posts / rolling 24h, per account** (not the commonly-cited 25). Query live usage via `GET /<IG_ID>/content_publishing_limit`. Carousels count as one post. Miles above the 4x/week target even with FB included. | `docs/instagram-platform/content-publishing/` |
| **M4** | Scheduling window | **No native scheduling exists.** `scheduled_publish_time` is whitelist-gated/non-functional for Instagram. The M6 runner (external, Task-Scheduler-driven) must trigger the actual `POST /media_publish` call at the intended time — which is exactly what §1.2/§9 already designed, so no rework, just confirmation the runner isn't optional. | Community-confirmed against current docs; official docs omit the parameter entirely — treat as closed, not "coming soon." |
| **M5** | App Review required for Page posting? | **No.** "Standard Access" — an app serving only accounts you own/manage — requires **no App Review, no Business Verification**. That tier only applies to apps managing *other people's* accounts. | `docs/instagram-platform/create-an-instagram-app/` |
| **M6** | Token lifecycle | Long-lived token: **60 days** from issue. Refresh via `GET /refresh_access_token?grant_type=ig_refresh_token...` once the token is **≥24h old** — **not automatic**; the runner must check token age and refresh proactively before every scheduled run, same shape as `EtsyTokenStore`'s rotate-on-use pattern. | `docs/instagram-platform/reference/refresh_access_token/` |
| **M7** | Can a published post be deleted (undo)? | **Yes** — `DELETE /<IG_MEDIA_ID>`. Whole carousel only, not individual items within one. Confirms the T0/T1 undo mechanism in §9/§10 is buildable as designed. | `docs/instagram-platform/reference/instagram-media/` |

**Net effect on the design:** no architectural rework needed — the external-runner model, the undo mechanism, and the single-adapter premise all check out against real API behavior. Two things to make explicit in slice 2/3: (a) auth uses **Facebook Login + Page Access Token**, not Instagram Login, so both publishers share one token; (b) the runner must proactively refresh the token (age check + refresh call) before each scheduled batch, since Meta does not auto-refresh.

**Not yet separately verified:** Facebook Page-specific posting rate limits (the 100/24h figure is Instagram Content Publishing specifically) — low priority given cadence is only 4x/week combined, but worth a quick check during slice 2 if Facebook posting volume ever grows independently of Instagram's.

---

## 8. Smallest Test That Proves It Works

**`tests/pipeline/meta/test_e2e_cadence.py`** — one integration test, FakeMeta, zero network:

```
# Seed: Gate 3 approval event
hero_id=1, image_path valid, caption empty, facebook_post=true

# Config: cadence=4/week, cooldown=6h

# Act: meta.schedule() dry-run (no Graph API)
# Assert: Two meta.post.proposed (IG + FB), neither scheduled yet (T2 = propose only)

# Act: Approve both proposals
# Assert: Two meta.post.scheduled, scheduled_for respects 6h cooldown, no API calls yet

# Act: Set SHOPSTEWARD_LIVE_META_WRITE=1, run --live-meta-write
# Assert: FakeMeta shows both as scheduled (Graph API calls executed)

# Act: meta.undo(post_id) on one
# Assert: meta.post.undone appended, FakeMeta state reverted for that post only,
#         second post remains scheduled

# Invariants:
# - Cadence logic (4x/week, 6h apart)
# - T2 tier (propose before approve before schedule)
# - Undo reversibility (byte-identical state restore)
# - Symmetric IG+FB (facebook_post=true routes both)
```

If any invariant breaks, test fails.

---

## 9. Implementation Slices (M6a ~2 weekends)

All slices assume M5a completion (hero path with Gate 3). Slices 1-4 independent of M8 until slice 4.

| Slice | Scope | Size | Mergeable |
|---|---|---|---|
| **0** | **Precondition.** Operator answers M1-M7 from Meta docs (§7). Record in PRD decision log. | 1 evening | — |
| **1** | `adapters/meta/interface.py` (protocols + MetaPost models), `FakeMeta` (complete mock), `MetaAuthStore` (token storage, no auth flow yet). | 1 evening | Interfaces + fakes only |
| **2** | **FIRST PR.** Full auth: `MetaAuthStore.oauth_flow()` (localhost redirect, token exchange). Live `LiveMeta` adapter (Graph API schedule_post / unschedule_post / get_analytics via httpx). FakeMeta tests comprehensive. | 1 weekend | Full auth + adapter tests, zero network. **Operator review required (new service).** |
| **3** | Core logic: `pipeline/meta/models.py` (event models), `projections.py` (queues), `runner.py` (propose -> govern -> schedule). `config/defaults/meta.json` seeding. Cadence + cooldown logic. | 1 weekend | Runner + cadence + projection tests on FakeMeta |
| **4** | Gate 3 integration: `hero.approved` event triggers `meta.propose()`. Tie M6 into M8 Brief (slice 4 of M8a): "Posted to IG/FB overnight" section. | 1 evening | Gate 3 integration tests. M8 dependency. |
| **5** | **Live smoke test (gated).** Schedule one real post on Eric's IG account via CLI, verify appears as "scheduled" in IG UI, undo via CLI, verify reverted. Record fixture. | 1 evening | none (live only, hand-verified). **Gate 5 approval before production.** |

**M6a ENDS here.** Includes: auth, adapter, core logic, Gate 3 tie-in, M8 Brief integration, CLI experience, live smoke test. UI dashboard is M6b.

---

## 10. Rollback Criteria

**Kill switch:** `meta.enabled=false` in `config/defaults/meta.json` -> runner no-ops entirely. No schema to unwind, no events to delete.

**Revert if:**

- (a) An `meta.post.published` event appears with no preceding `meta.post.approved` (authorization bypass)
- (b) A post publishes to the wrong platform (IG when intended for FB)
- (c) An undo fails to restore pre-scheduled state as verified against FakeMeta
- (d) Caption contains the operator's API key or any token (auth leak)
- (e) Meta blocks the app or token (account-suspension risk, per M8 §13.1)
- (f) More than two posts scheduled for the same hour despite cooldown rules (scheduler bug)
- (g) Operator rejects >50% of proposed posts over 20 proposals (recommendations are noise)

---

## 11. Architectural Rationale: `adapters/meta/` Decision

**Current state:** CLAUDE.md line 29 and PRD §7.1 both mention `adapters/instagram`. M8 draft §8.2 recommends `adapters/meta/`.

**This design chooses `adapters/meta/`.** Reasons:

1. **API boundary truth:** Meta Graph API v18.0 is ONE API. One OAuth token, one app ID, one rate limit. IG Business and linked FB Pages are two surfaces on the same platform.

2. **Future-proofing:** M8 contemplates Reels (IG + FB Feeds). Later, M8c Meta ads use the same Graph API. Renaming later costs import changes + import-linter amendment + §8.2 re-review. Naming right now costs one doc paragraph + CLAUDE.md/PRD amendments.

3. **Precedent:** `adapters/pod/` houses Gelato + Printful under one module because they are fulfillment partners. IG + FB are promotional surfaces under one API; same logic.

**Amendment required (if approved):**
- CLAUDE.md line 29: change `adapters/instagram` -> `adapters/meta`
- PRD §7.1: change `adapters/instagram` -> `adapters/meta`

---

## 12. PRD §13 Decision Candidates (80-83)

```
M6 design (architect 2026-08-05, READY FOR OPERATOR REVIEW):

80. Instagram scheduled posting cadence (4x/week, one tap per post per PRD §3.1)
    is EXECUTION of a Gate 3 approval, NOT a fourth gate. Operator approves the
    Etsy listing + IG asset (photo + caption) at Gate 3; M6 runner batches posts
    to meet the 4x/week target and publishes via Meta Graph API. Every post is
    undoable within 7 days via the M8 Brief (tier T0 for IG, T1 for FB).
    This preserves the three-gate principle: curate, finish, publish -- no new
    creative touchpoint.

81. Build src/shopsteward/adapters/meta/ (single module, two protocols:
    IgPublisher + PagePublisher), NOT separate adapters/instagram/ and
    adapters/facebook/. The Meta Graph API is one API, one OAuth token, one
    rate-limit boundary. Configuration controls per-surface (instagram.enabled,
    facebook.enabled); architectural seam is single. This amends CLAUDE.md and
    PRD §7.1 (both currently say adapters/instagram). Naming decision finalized
    in M6, not deferred to M8.

82. Instagram caption + hashtag generation REUSES adapters/copy infrastructure
    (OpenRouter transport, anthropic/claude-sonnet-5 default). Extend CopyInputs
    with intent field ("etsy_listing" or "ig_caption"). House style guide
    includes ig_caption template. Configuration controls: generate at Gate 3
    (alongside listing copy) or lazily at schedule time (if blank). Avoids
    adapter duplication; keeps all LLM under existing $10/mo soft cap (decision
    36, 38).

83. All Meta Graph API interaction uses httpx only -- no Meta SDK. OAuth follows
    EtsyTokenStore pattern (localhost redirect on `shopsteward meta auth`, token
    rotation per Meta lifecycle). Tokens in data/meta_tokens.json, never in
    event log. Live-write gating matches M5a Etsy exactly: three controls
    required (SHOPSTEWARD_LIVE_META_WRITE=1 env, META_API_KEY present, --live-meta-write
    CLI flag). Missing any control = runner refuses with actionable error.
```

---

## 13. Operator Actions Required Before Code

1. **Read Meta Graph API documentation** (v18.0, Business Help Center). Answer M1-M7 (§7). Record in PRD decision log.
2. **Approve or amend architecture:** Is `adapters/meta/` acceptable? Changes to interface design?
3. **Decide: Facebook in M6a or M6b?** M2 and M5 must be answered first. M6a can ship IG-only; M6b adds FB. Or both in M6a.
4. **Confirm three-gates principle is preserved** (§1.2 explains why posting is execution, not creation).
5. **Schedule C-Suite critique panel** (PRD §8.3). **Chief Legal reviews §7 (policy questions) and §11 (account suspension risks per M8 §13.1).**

---

**Design ready for operator review. No code written. Awaiting approval before proceeding.**
