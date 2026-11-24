# Design: Buffer social adapter (replaces the Claude Desktop Buffer-MCP job)

**Author:** `architect`, 2026-11-22. **Status:** proposal. Delegated under the CLAUDE.md
governance (adapter, external service, dependency choices). Operator items are only in §9.
**Supersedes:** the 2026-08-24 roadmap P3 (Pinterest write `live.py`) and P5 (Meta `live.py`)
for *publishing*. Buffer is already connected to all three channels, so no Pinterest
OAuth app and no Meta App Review are needed. `adapters/pinterest` and `adapters/meta`
stay parked. The M6 design's "rejected: third-party scheduler" row is reversed for the
reasons in §2. Its two objections (state kept in two places, undo not 1:1) are handled by
`buffer.post.observed` (§6) and the undo window (§4).

Sources: developers.buffer.com `.md` guides and the `reference.md` schema, fetched
2026-11-22 and used as reference data only. Unverified facts are listed in §10.

## 1. Decisions at a glance

| Question | Decision |
|---|---|
| Live transport | **httpx GraphQL** in `adapters/buffer/live.py`, key read from `BUFFER_API_KEY`. The `@bufferapp/cli` stays an operator, debug and fixture-recording tool. Never shell out. |
| Image hosting | Buffer has **no upload endpoint**. It fetches `assets[].image.url` *at publish time* and warns against presigned or expiring URLs. **Use Etsy CDN URLs** (public, stable, https). **Do not reuse `adapters/printfile`.** Its R2 URLs are presigned and expire, and that bucket holds sellable files, so it must never be made public. |
| Where publishing lives | **Variant B inside `social.pinterest_post` and `social.caption_draft`**, through one shared helper `pipeline/ops/buffer_publish.py`. No new capability. |
| Tiers (publisher present) | pinterest_post: `max_tier` **NOTIFY** (as the 2026-08-24 §2.3 design specified). caption_draft: **NOTIFY**, which **reverses** its documented "never promotable" pin (§4 note). The ladder still starts both at PROPOSE. With no publisher, both are unchanged (PROPOSE, draft-only). |
| Undo | `deletePost` works **only while the post is queued**. Deleting a post in Buffer does not remove it from the network. Undo after send raises a clear "remove it on the platform" error. The NOTIFY window is enforced: `customScheduled` with `dueAt` at least `min_lead_hours` ahead. |
| Idempotency | `createPost` has **no idempotency key**, and a `refused` action is terminal for `ops run`, so nothing retries automatically. The drafted event is appended *before* the call as an intent marker. An unresolved intent blocks its (listing, channel). `buffer-sync` reconciles it, and ambiguity always means "do not create". Details in §5. |
| Cadence | `config/defaults/ops.json` -> `buffer.channels.*.posts_per_week`. The governor adds a per-channel `PACE` refusal that counts **all** posts on the channel, including those from the Desktop job. |
| Metrics | `shopsteward ops buffer-sync` appends `buffer.post.observed`. On first `sent` it auto-appends the existing `social.pin_posted`/`social.caption_posted` with `posted_at = sentAt`, so `proj_pin_experiments` works unchanged. |

## 2. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Shell out to `@bufferapp/cli` | Adds Node to the runtime. The key would live in the CLI's global config, outside our env and gate model. Tests would mean subprocess mocks instead of respx. Exit codes are coarser than GraphQL's typed `MutationError` union. Keep the CLI for humans. |
| New `social.buffer_publish` capability that consumes `*_drafted` events | Two action_ids per post. During the ladder ramp every post needs two approvals (draft, then publish), because a fresh capability always starts at PROPOSE. It also needs a drafted-event backlog consumer. Variant B gives one tap and one action_id (= utm join key) per post. |
| Direct Pinterest v5 and Meta Graph live adapters | Need an OAuth app, Business Verification and App Review for the same three channels Buffer has already authorized. The 2026-08-24 register put these on the operator's paperwork path. |
| Public R2 bucket for every post | Not needed for listing posts, because the Etsy CDN already hosts them. Deferred to slice S10, for non-listing photos only. |

## 3. `src/shopsteward/adapters/buffer/`

Pinterest-adapter shape: read Protocol + write Protocol + error class + models + fake. No IDs
in config, docs or fixtures (public repo). Channels are keyed by service name and IDs are
resolved at runtime.

```
models.py
  BufferChannel     channel_id, service ("instagram"|"facebook"|"pinterest"), name,
                    is_disconnected, is_locked, is_queue_paused,
                    boards: dict[str, str]            # pinterest board name -> serviceId
  BufferPostSpec    channel_id, text, image_url: str | None,
                    mode: "addToQueue"|"customScheduled", due_at: datetime | None,
                    scheduling_type: "automatic"|"notification",
                    pin_title, pin_url, pin_board_service_id: str | None,
                    tag_ids: list[str] = [], ai_assisted: bool = True
  BufferPost        post_id, channel_id, status, text, due_at, sent_at, created_at,
                    external_link, pin_url, error_message: str | None,
                    metrics: dict[str, float]         # ABSENT != 0, never zero-filled
                    metrics_updated_at: datetime | None
interface.py
  class BufferAdapter(Protocol):                      # read
      def list_channels(self) -> list[BufferChannel]: ...
      def list_posts(self, *, channel_ids: list[str], created_since: datetime,
                     statuses: list[str] | None = None,
                     with_metrics: bool = False) -> list[BufferPost]: ...
  class BufferError(RuntimeError):    # code: UNAUTHORIZED|FORBIDDEN|NOT_FOUND|RATE_LIMITED|MUTATION|TRANSPORT
      code: str; retry_after_s: int | None   # message truncated to 500 chars, never the raw body
  class BufferUnknownOutcome(BufferError)    # timeout/5xx after send: the post MAY exist -> reconcile
  class BufferWriteAdapter(Protocol):
      def create_post(self, spec: BufferPostSpec) -> BufferPost: ...
      def delete_post(self, post_id: str) -> None: ...
fake.py  FakeBufferAdapter (read+write, in-memory): create mints post-{n}, counts creates,
         fail_after_create_once simulates a timeout AFTER a successful create.
live.py  httpx.Client(base_url="https://api.buffer.com", bearer). HTTP 200 + errors[] ->
         BufferError(code); MutationError union member -> BufferError("MUTATION"); 429 ->
         BufferError("RATE_LIMITED", retry_after_s), no sleep when > cfg max_retry_after_s
         (a 30-day Retry-After must refuse, never block). Timeout/5xx on a mutation ->
         BufferUnknownOutcome. Org id fetched once per process.
```

Deliberately absent: `editPost`, ideas, notes, content items (experimental), tag CRUD (the
operator creates the `shopsteward` tag once; we resolve it by name), and any ads surface.
Gate: `pipeline/live_gate.live_buffer_open()` requires `SHOPSTEWARD_LIVE_BUFFER=1` plus
`BUFFER_API_KEY`, and `cli.py` builds `live.py` only under `--live-autonomy`. That is the
existing triple-gate pattern. No new dependency (httpx and respx are already in-tree).

## 4. Capability changes (Variant B)

Mode is per instance: `SocialPinterestPost(publisher=None)` / `SocialCaptionDraft(publisher=None)`.
**`publisher is None` is exactly today**: `max_tier = PROPOSE`, `undo = None`,
`policy_verified = True`, and draft only. Instance attributes are set in `__init__`.

| With a publisher | `social.pinterest_post` | `social.caption_draft` |
|---|---|---|
| `max_tier` / `undo` | NOTIFY / `buffer_publish.undo()`: deletePost while still `scheduled` + `social.buffer_deleted`; raises once sent | same |
| `policy_verified` | True once policy rows B1/B2 exist (2026-08-24 §2.4) | True once B1/B3 exist **and** §9 Q1 is answered |
| materialize | stamps `params.publish_via = "buffer"` | same |
| Image | Etsy `url_fullxfull` (S3), else `url_570xN`. Drop if long edge < `min_long_edge_px`. | Same. Instagram also drops aspect outside `[aspect_min, aspect_max]`. **Drop, never crop**; no image transform anywhere. |
| Link | existing `_destination_url()` -> `metadata.pinterest.url` | facebook: deterministic UTM listing URL appended (`append_link`, utm_source=facebook); instagram: none |

**Publish intent without a publisher.** If an action stamped `publish_via=buffer` is approved
without `--live-autonomy`, it raises (refused; `ops approve` can re-govern it later). It never
silently executes as a draft. Neither key joins `LIVE_GATED_CAPABILITIES`, because that would
block the offline draft path, and no fake adapter is ever wired in production code. `undo()` of
an action with `after.buffer_post_id` but no publisher raises `LiveGateBlockedError`. Both
registration sites (`run_cmd`, `_register_autorenew`) pass the same publisher.

**Caption tier reversal (overturns the documented `caption_draft` decision).** The
"operator glance on owned-audience copy" rationale is now *earned off by the ladder* instead
of hard-pinned in Python: the action starts at PROPOSE, needs 20 approvals with 0 rejections
over 14 days, and any rejection or undo demotes it. The pin predates the 2026-08-24 governance
rewrite. A queued post is deletable inside an enforced lead window, so this is not an
irreversibility case like `seo_edit`.

**Governor.** The pin/seo holdout is unchanged (same key). New per-channel `RefusalReason.PACE`:
refuse when the ISO-week count (observed Buffer posts on the channel from *any* source, sent +
scheduled, plus local `social.buffer_queued` not yet observed) >= `posts_per_week`, or when the
scheduled count >= `max_queue_depth`. A channel that is disconnected, locked, paused, ambiguous
or missing gives `PRECONDITION`.

## 5. Execute and idempotency (`pipeline/ops/buffer_publish.py`)

```
execute(action):          # publish_via=buffer path
  if social.buffer_queued exists for action_id: return      # FIRST, before revalidation
  revalidate (existing _stale_check / _candidates)          -> StaleTargetError
  due_at = next config post_times_utc slot >= now + min_lead_hours, not already taken on channel
  append social.{pin|caption}_drafted {..., publish_via: "buffer"}   # intent + copy record
  post = publisher.create_post(spec, mode=customScheduled, due_at)
      BufferUnknownOutcome -> one inline reconcile(); found -> continue; else raise (refused)
  append social.buffer_queued {action_id, buffer_post_id, channel, due_at}

candidates: (listing, channel) with an UNRESOLVED intent (publish_via=buffer drafted, no
  buffer_queued and no buffer_lost) is never a candidate, whatever the cooldown says.

buffer-sync reconcile(intent):   list_posts(channel, created_since = drafted_at - 5 min)
  (no tag filter: the tag is optional), matched locally on
    pinterest: pin_url utm_content == action_id[:12]
    facebook:  utm_content == action_id[:12] inside text
    instagram: exact text == caption
  exactly one match -> social.buffer_queued {reconciled: true}
                       (Brief: "posted despite refusal, delete via Buffer/CLI if unwanted")
  zero matches AND older than reconcile_window -> social.buffer_lost (releases the pair)
  anything else (ambiguous, text normalized, V4 fails) -> stays unresolved; Brief flags it
```

Ambiguity never creates. A false "lost" is the only double-post path, and it needs a channel
with zero posts in the window that would match.

**New event types:** `social.buffer_queued`, `social.buffer_deleted`, `social.buffer_lost`,
`buffer.post.observed`, `buffer.channels.observed`. **Reused:** `social.pin_drafted` and
`social.caption_drafted` (+ `publish_via`), `social.pin_posted` and `social.caption_posted`.
All are append-only and carry `user_id`.

## 6. Metrics and the experiment loop

`shopsteward ops buffer-sync` is read-only, and the scheduler runs it right before `ops run`:
1. `list_channels()` -> `buffer.channels.observed`, refreshed daily (the Buffer efficiency guide says to cache channels).
2. `list_posts(three channels, created_since = now - 35d, with_metrics=True)` -> one `buffer.post.observed`
   per post whose status or metrics changed. This is 1-3 requests. It also runs the §5 reconcile.
3. On the first `sent` for a post mapped via `social.buffer_queued`, call the existing
   `mark_posted(..., posted_at=sent_at)`. It is idempotent, and the pin readout then anchors on
   the real send time with no code change.
4. `status=error` -> a line in a Brief `social_queue` section. It is never retried automatically.

`pin_experiment_readout` gains the latest Buffer metrics per row (impressions, saves, reactions),
with **absent != 0**. A sibling `caption_experiment_readout` reuses the views-delta method on
`social.caption_posted`. Buffer has **no link-click metric** (`link_clicks` is deprecated), so
conversion stays as the Etsy views delta plus UTM. Roadmap P2 (Pinterest outbound clicks) stays
valid at a lower priority.

## 7. Config (`config/defaults/ops.json`, no IDs)

```json
"buffer": {
  "tag_name": "shopsteward", "min_long_edge_px": 1000, "reconcile_window_minutes": 60,
  "max_retry_after_s": 60, "sync_lookback_days": 35, "min_lead_hours": 4,
  "channels": {
    "pinterest": {"service": "pinterest", "posts_per_week": 7, "max_queue_depth": 8, "post_times_utc": ["14:00"]},
    "instagram": {"service": "instagram", "posts_per_week": 4, "max_queue_depth": 8, "post_times_utc": ["16:00"], "aspect_min": 0.8, "aspect_max": 1.91},
    "facebook":  {"service": "facebook",  "posts_per_week": 4, "max_queue_depth": 8, "post_times_utc": ["17:00"], "append_link": true}
  }
},
"autonomy": {"planner_max_per_run_overrides": {"social.caption_draft": 2, "social.pinterest_post": 2}}
```

Instagram 4/week is the PRD §3.1 target. `scheduling_type` is always `automatic` (see V3).
Pinterest `board_key` resolves through the existing `pinterest.boards` name map to
`BufferChannel.boards[name]` at runtime. The planner facts gain `cadence_deficit: {channel: n}`,
so (2) "writes its own posts" is driven by the deficit. The LLM writes copy only; SQL picks the
targets (the existing coverage-first and proven policies).

## 8. Policy, budget, fixtures

| Item | Position |
|---|---|
| Buffer terms | A personal API key acting on its own account is the documented use case ("personal workflows and automations"). Metrics require a personal key. |
| Pinterest spam rule | The existing per-listing cooldown, plus `posts_per_week`, plus coverage-first ordering. |
| Spend | Buffer Free plan: 0 USD, `estimate_cost_usd = 0.0`, and the 20 USD/month cap is untouched. The adapter has no ads surface. |
| Rate limits (Free, 3 channels): 150/15 min, 600/24 h, 3000/30 d, **shared with the CLI and the Desktop MCP** | About 15 writes and 30 reads a week, roughly 6-7% of the 30-day budget. The remaining counts from the `RateLimit` header go into the run report. |
| Policy doc rows | **B1** Buffer API automation on own account. **B2** Pinterest via Buffer (spam/cooldown). **B3** IG/FB via Buffer (an approved Meta partner). |
| `aiAssisted: true` | Always set; it is true of the copy. `isAiGenerated` is §9 Q1. |
| Fixtures | Record once (CLI or httpx): `channels`, `posts`+metrics, and `createPost`/`deletePost` with `saveToDraft: true` (never publishes). Replace every ID, `serviceId`, handle, display name, avatar, `externalLink` and image URL with `fx-*` tokens. Commit only under `tests/fixtures/buffer/`. A guard test fails on 24-hex IDs or on `buffer.com/`, `instagram.com/` or `etsystatic` strings. |

## 9. Operator Review Boundary items (the only things that wait on a human)

| # | Boundary | Item |
|---|---|---|
| Q1 | Legal/platform | Does the 2026-08-11 Etsy determination (generative *removal* = editing, not "AI-generated") also cover the **Meta and Pinterest AI-content labels**, so `isAiGenerated` stays false and no AI label is applied? Until answered, live captions keep `policy_verified = False` (governor: `POLICY_UNVERIFIED`), and live pins too if the answer covers Pinterest. |
| Q2 | Security | Put the existing Buffer key into the ShopSteward env as `BUFFER_API_KEY`. The Free plan allows **one** key, shared with the CLI and the Desktop MCP, so rotating it breaks all three. |
| Q3 | Budget-expansion (conditional) | Only if V1/V2 show that metrics or an 8-deep queue need a paid plan. The default stays on Free and degrades: queue depth is config, and metrics are skipped if unavailable. |
| Q4 | Security (deferred, S10) | Non-listing photo posts need a **new public R2 bucket and credential**, never the print-file bucket. Ask only when S10 is reached. |

**Resolved 2026-11-22 (operator):** Q1 = yes, the Etsy ruling covers Meta and Pinterest too (removal is editing; no AI label, `isAiGenerated` false). Q2 = the operator adds the same existing key to `.env` as `BUFFER_API_KEY`, shared until S9 retires the Desktop job.

Disabling the Desktop job (S9) is a cutover step the operator performs. It is not a boundary.

## 10. Facts to verify (read-only, during S2 fixture recording)

V1 The Free-plan cap on scheduled posts per channel (and that it surfaces as `LimitReachedError`).
V2 `Post.metrics` is readable on Free. V3 The IG channel supports `automatic` publishing
(Business/Creator); if it is `notification`-only, IG posting stays manual. V4 `text` round-trips
byte-exact; if not, IG reconcile never resolves to "lost" (safe, but noisy). V5 Buffer can fetch
`i.etsystatic.com` fullxfull URLs. V6 Instagram behaviour for out-of-range aspect: reject or
auto-crop. V7 `deletePost` on a sent post removes only the Buffer record. V8 Paid-plan pricing
per channel, if Q3 ever triggers.

**Verified 2026-11-23 (live, read-only, scrubbed fixtures `tests/fixtures/buffer/recorded_*.json`):**
3 channels (facebook, pinterest with 2 boards, instagram), none disconnected,
locked or paused. V2 = yes: metrics are readable on Free (16 of 18 posts in
60 days had them). Buffer sends display labels ("Impressions", "Pin Clicks",
"Eng. Rate", "Saves", ...); the adapter keys them snake_case (`impressions`,
`pin_clicks`, `eng_rate`). The 30-day rate-limit header read 2984 of 3000
remaining. V1, V3-V7 still open: they need a write or a channel field the
query doesn't select (V3).

## 11. Slices (each is one PR with its acceptance test)

| # | Slice | Acceptance test |
|---|---|---|
| S1 | `adapters/buffer` interface/models/fake | create, list, delete; a second delete raises `BufferError(NOT_FOUND)`; a post with no metrics has `metrics == {}` |
| S2 | `live.py` + `live_buffer_open()` + scrubbed fixtures | respx: errors[] maps to code; MutationError maps to MUTATION; 429 with Retry-After 2592000 raises at once; a create timeout raises `BufferUnknownOutcome`; the scrub guard passes |
| S3 | Etsy images payload adds `url_fullxfull`, `full_width`, `full_height` (additive); `_image_url` moves to `ops/social.py` | old events still resolve to 570xN; new events prefer fullxfull |
| S4 | `ops buffer-sync` (read-only; goes live first and already observes Desktop-job posts) | a sent post mapped to a pin action yields exactly one `social.pin_posted` with `posted_at == sent_at`; a re-sync appends nothing |
| S5 | pinterest_post Variant B + `buffer_publish.py` + rows B1/B2 | fake `fail_after_create_once` with the post findable: execute returns, and `create_count == 1`. With the post NOT findable: refused, the pair blocked, and the next sync reconciles to `buffer_queued{reconciled}`, with no second create. Publisher None: byte-identical to today. |
| S6 | caption_draft Variant B (FB link, IG aspect drop) + B3 | an IG listing with aspect 0.66 is never a candidate; FB text ends in a UTM URL with `utm_content == action_id[:12]`; `due_at >= now + min_lead_hours` |
| S7 | Cadence: governor `PACE`, `cadence_deficit` facts, per-run overrides | 3 observed posts (2 foreign) + 1 local unobserved `buffer_queued` with `posts_per_week: 4` gives `PACE` |
| S8 | Brief `social_queue` (due/undo-until per post, deficits, unresolved intents) + readout metrics | absent impressions render "not yet reported", never 0 |
| S9 | Cutover: a week of `ops run --dry-run` beside the Desktop job, then the operator disables the job | proposals per channel <= the deficit, and no (listing, channel) duplicates an observed post |
| S10 | (deferred) public media host for non-listing photos | only after Q4 |

**Smallest proof overall:** the S5 test. A timeout after a successful create never yields a
second Buffer post.
**Rollback:** unset `SHOPSTEWARD_LIVE_BUFFER`; both capabilities return to today, byte-for-byte.
`ops undo` deletes posts that are still queued. Re-enable the Desktop job. Events stay. Trigger on
any duplicate post, any post to an unintended channel, an error-status rate above 1 in 10 over
a week, or caption_draft being demoted twice.

## 12. C-suite critique (PRD §8.3)

- **CTO.** Swapping a flaky MCP hop for a respx-tested httpx client plus the event log is right.
  The weak spot is Instagram reconcile on exact text alone. *Improvement:* if V4 shows tags work on
  Free, match on tag + createdAt first and fall back to text. Add a test that checks the S2
  fixtures against a committed schema snapshot.
- **CFO.** Zero marginal cost, nothing against the monthly cap. The hidden cost is a rate budget
  shared with the CLI and the MCP. *Improvement:* store the remaining `RateLimit` counts on
  `buffer.channels.observed`, and have the Brief warn below 25% of the 30-day budget, before any
  paid-plan talk.
- **CMO.** Owned IG/FB drove about 75% of historical sales, so cadence beats polish, but Instagram
  has no clickable link. *Improvement:* add an optional IG `firstComment` plus a config "link in
  bio" line, and point the bio link at a UTM-tagged shop URL, so IG is attributable as a channel.
- **CPO.** The operator job is "the queue stays full and I am told what went out", so the Brief is
  the product. *Improvement:* show "due <time>, undo until <time>" per post, put the per-channel
  deficit and unresolved intents first, and post nothing silently.
- **Chief Legal.** An own-account key is inside Buffer terms, and the deterministic UTM keeps the
  LLM away from destinations. Platform AI labelling is the open exposure. *Improvement:* answer Q1
  in writing in the policy doc before S6 goes live, and record `ai_label_applied: bool` on every
  `social.buffer_queued` event as a per-post audit trail.
