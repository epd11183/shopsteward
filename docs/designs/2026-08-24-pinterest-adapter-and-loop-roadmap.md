# Design: Pinterest adapter + `social.pinterest_post` + loop-closing roadmap

**Author:** `architect` sub-agent, 2026-08-24. **Status:** items 1-3 of the register in §4 (interface/models/fake, the capability, config additions) were approved by the operator 2026-08-24 ("do it"). **Register reconciled 2026-08-25** against the 2026-08-24 governance rewrite in `CLAUDE.md` — most of what items 4-9 called "not approved" is ordinary delegated engineering under those rules. What actually still requires the operator is narrow and specific: obtaining a Pinterest business account, creating the developer app, granting OAuth scopes, and storing the resulting credential (Security boundary — credential and account ownership), plus Meta's Business Verification (Business-identity boundary). Everything else in 4-9 — writing `live.py` against a settled interface, choosing an image-gen provider, adding a dependency — proceeds without an approval gate. See §4 for the per-row reconciliation.

**Autonomy note (2026-08-24 operator direction):** the operator explicitly directed more autonomy, not less, subject to two fixed limits (see `CLAUDE.md`'s 2026-08-24 amendment): capabilities hard-pinned to `Tier.PROPOSE` in Python stay operator-approved (irreversibility, not trust), and the $20/month spend cap is never raised unilaterally. Everything else defaults toward more autonomy. Per that direction, `social.pinterest_post`'s eventual live-posting variant (Variant B, §2.3) is designed for **`Tier.NOTIFY`**, not `Tier.PROPOSE` as originally drafted below — reasoning: a pin is free, individually deletable via `delete_pin`, and does not touch Etsy's search-ranking history the way `seo_edit` does. This is noted inline in §2.3.

---

## 0. The headline recommendation, first

**Do not build the Pinterest live integration first.** Build the capability first in `caption_draft`'s exact assist-only shape — it emits a `social.pin_drafted` event (title, description, board name, destination URL with UTM) that the operator pastes into Pinterest by hand. Zero new external service, zero OAuth, zero sign-off needed, works this week.

Reason: the expensive, sign-off-gated part (OAuth app, Business account, Trial→Standard access review) buys *posting throughput*. Throughput is worthless until we know the LLM's pin copy converts at all, and at 27 listings the manual posting cost is ~10 minutes a week. Spec the adapter now (below) so the interface is settled and reviewed; wire `live.py` only when manual posting has produced enough outbound clicks to justify it.

The interface spec below is still worth landing as `interface.py`/`models.py`/`fake.py` — it's the contract the eventual live implementation and the analytics ingestion both code against, and `fake.py` lets the capability's tests run end-to-end today.

---

## 1. `src/shopsteward/adapters/pinterest/`

Shape mirrors `adapters/etsy/`: read Protocol + write Protocol + a single error class + Pydantic models + fixture fake. No `live.py`.

### 1.1 `models.py`

```
PinterestBoard        board_id: str, name: str, description: str = "",
                      privacy: BoardPrivacy ("PUBLIC"|"PROTECTED"|"SECRET"),
                      pin_count: int = 0, follower_count: int = 0
BoardSpec             name: str, description: str = "", privacy: BoardPrivacy = PUBLIC
PinterestBoardRef     board_id: str            # create_board return, EtsyListingRef twin
PinMedia              source_type: "image_url" | "image_base64"
                      url: str | None; data_b64: str | None; content_type: str | None
PinSpec               board_id: str, media: PinMedia, link: str,
                      title: str (<=100), description: str (<=800),
                      alt_text: str = "" (<=500), note: str = ""
PinterestPin          pin_id: str, board_id: str, link: str | None, title: str,
                      description: str, created_at: datetime,
                      media_url: str | None
PinterestPinRef       pin_id: str
PinMetric (StrEnum)   IMPRESSION, PIN_CLICK, OUTBOUND_CLICK, SAVE
PinAnalytics          pin_id: str, start: date, end: date,
                      metrics: dict[PinMetric, int]     # absent metric = absent, never 0
AccountAnalytics      start: date, end: date, metrics: dict[PinMetric, int]
```

`metrics` as a dict with **absent ≠ zero** deliberately mirrors `analytics._views_delta`'s `None`-means-unmeasurable rule — a missing metric window must never render as "0 clicks, this pin failed."

`PinMedia` carries both variants because Etsy CDN image URLs (`url_570xN`) are public and cheap (`image_url`), but a locally-composited mockup is not (`image_base64`). Both are real Pinterest v5 `media_source` types.

### 1.2 `interface.py`

```python
class PinterestAdapter(Protocol):          # read
    def list_boards(self) -> list[PinterestBoard]: ...
    def get_board(self, board_id: str) -> PinterestBoard: ...
    def list_pins(self, board_id: str) -> list[PinterestPin]: ...
    def get_pin(self, pin_id: str) -> PinterestPin: ...
    def pin_analytics(
        self, pin_id: str, *, start: date, end: date,
        metrics: list[PinMetric],
    ) -> PinAnalytics:
        """Returns a PinAnalytics whose `metrics` omits any metric Pinterest
        did not report for the window. A pin younger than Pinterest's
        analytics lag reports nothing -- that is absence of data, never zero."""
    def account_analytics(
        self, *, start: date, end: date, metrics: list[PinMetric],
    ) -> AccountAnalytics: ...


class PinterestWriteError(RuntimeError):   # EtsyWriteError twin, verbatim shape
    def __init__(self, status_code: int, error: str | None) -> None: ...
        # stores status_code/error, message truncated to _MAX_ERROR_LEN = 500,
        # never the raw response body


class PinterestWriteAdapter(Protocol):
    def create_board(self, spec: BoardSpec) -> PinterestBoardRef: ...
    def create_pin(self, spec: PinSpec) -> PinterestPinRef: ...
    def delete_pin(self, pin_id: str) -> None:
        """The real undo path for social.pinterest_post -- unlike
        social.caption_draft (nothing to reverse) a published pin is
        genuinely deletable. Also smoke-test cleanup."""
```

Deliberately **absent**: `update_pin`, `update_board`, `delete_board`, follow/comment/message endpoints, any Pinterest Ads surface. YAGNI, and the messaging/ads surfaces carry the same policy risk class as Etsy's E11/E10 — they should get their own policy verdict before they get an interface.

### 1.3 `fake.py`

`FixturePinterestAdapter(fixture_dir)` for reads (boards/pins/analytics loaded from scrubbed JSON, `list_pins` filtered by `board_id`), plus an in-memory `FakePinterestWriteAdapter` holding `dict[str, PinterestPin]` — `create_pin` mints `pin-{n}`, `delete_pin` pops and raises `PinterestWriteError(404, ...)` on a missing id. Same split `adapters/etsy/fake.py` already uses. No fixture may contain a real board/pin id or the shop's account id (public repo).

### 1.4 What the operator has to obtain (account/credential steps — a human has to click through them)

These are Security-boundary items under `CLAUDE.md` governance: account ownership, OAuth grants, and secret storage. They are operator actions because only a human can create an account and consent to a scope grant — not because the design or the code needs approval. Writing `live.py` against the interface below is delegated work; it is blocked only in the sense that it cannot be *exercised* without a credential.

Before `live.py` can run:

1. **A Pinterest *business* account** (converting the personal account is free and reversible). Pin analytics and API posting are business-only.
2. **A Pinterest developer app** at developers.pinterest.com. New apps start at **Trial access** — restricted to the app owner's own account with a low daily call ceiling. That is *sufficient for this shop*; we never need Standard access, which requires app review.
3. **OAuth 2.0 authorization-code flow** (`https://www.pinterest.com/oauth/`, token exchange at `https://api.pinterest.com/v5/oauth/token`), refresh tokens. Same storage handling as `adapters/etsy/auth.py`.
4. **Scopes to request** — request exactly these, no more:
   - `boards:read`, `boards:write`
   - `pins:read`, `pins:write`
   - `user_accounts:read` (required for the analytics endpoints)
   - **Not** the `*_secret` variants, **not** any `ads:*` scope.
5. A redirect URI registered against the local `shopsteward serve` callback.

Endpoint mapping for the eventual `live.py` (v5): `GET/POST /v5/boards`, `GET/POST /v5/pins`, `DELETE /v5/pins/{pin_id}`, `GET /v5/pins/{pin_id}/analytics`, `GET /v5/user_account/analytics`. Rate limits are per-app-per-user and modest; the capability's daily cap (below) keeps us orders of magnitude under.

**Rejected alternatives**

- *One combined read+write Protocol.* Rejected: Etsy and POD both split read from write, and the split is what lets the capability hold a write adapter while the analytics ingester holds only a read one. Keeps the blast radius of a write credential visible.
- *Model pins as `MetaPost` and reuse `adapters/meta`.* Rejected: Meta's model is queue-and-status (`PostStatus`, `scheduled_for`) because Meta publishing is scheduled; Pinterest v5 has no scheduling in the API and a pin has a destination link and a board, which `MetaPost` has no concept of. Forcing them together produces a union type that's wrong for both.
- *Generic `SocialAdapter` covering Pinterest + Meta.* Rejected outright — an interface with one real implementation and one unwired one, abstracting over two genuinely different publishing models.

**Guardrail impact:** none negative. New external service — a delegated decision under the 2026-08-24 governance rewrite; the operator-gated part is the credential (item 4 in §4). `fake.py` is the default, `live.py` doesn't exist yet. No dependency added — `live.py` would use `httpx`, already in-tree.

**Smallest test that proves it:** one `test_pinterest_fake.py` — create a board, create a pin against it, `list_pins` returns it, `delete_pin` removes it, second `delete_pin` raises `PinterestWriteError` with `status_code == 404`, and `pin_analytics` for a window with no data returns `metrics == {}` (not zeros).

**Rollback:** delete the package. Nothing imports it until the capability lands.

---

## 2. `social.pinterest_post` on the M8b chassis

### 2.1 Eligibility

**Do NOT gate on `analytics.top_sellers()`. Gate on "active, real, has an image, and hasn't been pinned recently," and order candidates by *least-recently-pinned* — an explore policy, not an exploit one.**

`top_sellers()` is the right gate for `gapfill_reprint` and `caption_draft` for a reason that **does not transfer**. Those two spend something scarce: `gapfill_reprint` creates catalog (a POD SKU, real per-unit base cost, operator attention at Gate 3), and `caption_draft` spends the shop's *audience attention* on a single feed post that dies in a day. When the resource is scarce and the shot is one-off, "only promote what's proven" is correct, and the proof requirement is what keeps the LLM from betting the shop on its own taste.

A pin is the opposite on every axis. It costs $0, it's individually deletable (real undo), it's one of dozens rather than one-of-one, and — the load-bearing difference — **a pin is a long-lived search-index entry, not a feed post.** Pins surface for months. The correct policy for a cheap, reversible, long-tailed, high-variance channel is *many small independent trials across the whole catalog*, and then read which ones caught.

And the decisive practical point, documented in `docs/research/2026-08-24-etsy-path-to-profitability.md`: gating on `top_sellers()` makes this capability **structurally inert at $0 revenue**. It would register, propose nothing, forever. The whole purpose of the channel is to break the chicken-and-egg; inheriting the gate that *causes* the chicken-and-egg would be a design error dressed up as consistency.

Two guardrails replace the sales gate:

- **Coverage-first ordering, not ranking.** `_candidates()` returns active listings sorted by `(last_pinned_at ASC NULLS FIRST, listing_id)`. The planner's per-run cap (`planner_max_per_capability_per_run`) then takes the top N. The LLM chooses *copy*, never *which listing wins*.
- **A cooldown.** A listing that already has a pin within `cfg.pinterest.cooldown_days` is not a candidate — the real anti-spam control (Pinterest's own policy treats repetitive pinning of the same destination as spam).

### 2.2 Module shape (`pipeline/ops/capabilities/pinterest_post.py`)

Planner-only, `caption_draft`'s pattern verbatim:

```
key            = "social.pinterest_post"
max_tier       = Tier.NOTIFY (live variant, per 2026-08-24 operator autonomy direction --
                 see header note; Variant A / draft-only has no execution risk at all)
policy_verified                          # NOT True until a Pinterest policy verdict exists
undo           = <real callable in the live variant> | None in the draft-only variant
propose()      -> []                     # no sensible deterministic pin copy
```

`_candidates(conn, user_id, cfg) -> dict[str, PinCandidate]` is **the one grounding function**, keyed by `str(listing_id)`, shared by `materialize()` and `execute()`. A listing is a candidate iff all of:

1. `proj_listings.state == "active"`,
2. a usable image exists — a `url_570xN` from the stored `etsy.listing.images.observed` projection (no live Etsy call inside `_candidates`),
3. no `social.pin_posted` / `social.pin_drafted` event for that `listing_id` inside `cfg.pinterest.cooldown_days`.

The LLM supplies only `params`: `title`, `description`, `alt_text`, `board_key`. Validation is structural and **drops, never truncates**: non-empty `str`, length limits per `_OpsPinterest` config, `board_key` ∈ `cfg.pinterest.boards` (a config-declared board map — the LLM can never invent a board name).

**The destination URL is computed deterministically by the capability, never by the LLM:**

```
https://www.etsy.com/listing/{listing_id}
  ?utm_source=pinterest&utm_medium=social
  &utm_campaign=shopsteward&utm_content={action_id[:12]}
```

`utm_content = action_id` prefix is the join key between a proposal and whatever traffic it drove. The LLM cannot construct, alter, or see this — it's derived from the grounded `listing_id`.

`estimate_cost_usd` → `0.0`.

### 2.3 Two variants — ship the first, gate the second

**Variant A — `social.pin_drafted` (assist-only, no adapter, ship now).** `execute()` appends one event with `{listing_id, title, description, alt_text, board_key, destination_url, image_url}`; the Brief grows a `pin_drafts` section. Holds no adapter. `undo = None`. **No new external service, no new dependency, no sign-off beyond the usual PR review.**

**Variant B — `social.pinterest_post` (live posting; blocked on a live Pinterest credential, not on approval).** Holds a `PinterestWriteAdapter` injected at construction. `execute()` calls `create_pin`, appends `social.pin_posted`. `undo()` calls `delete_pin` and appends `social.pin_deleted`. Registered in `cli.py` only under `--live-autonomy`. **`max_tier = Tier.NOTIFY`** per the 2026-08-24 operator autonomy direction (see header note) — a pin publishes automatically and the operator is notified after, rather than requiring a click before each one, reflecting that a pin is free, deletable, and doesn't touch Etsy's search-ranking history the way a title/tag edit does.

### 2.4 `policy_verified` is currently unanswerable

Every live capability sets `policy_verified = True` on the strength of a specific verdict row in `docs/policy/2026-08-11-autonomy-platform-policy.md`. **That document covers Etsy and Meta only — there is no Pinterest section.** Variant B must not set `policy_verified = True` until a Pinterest row exists (API terms + the "no repetitive/spam pinning of the same destination" community guideline, which the cooldown is designed against). Variant A needs nothing — it makes no Pinterest call at all.

**Rejected alternatives**

- *Gate on `top_sellers()` for consistency with its siblings.* Rejected — inert at $0 revenue.
- *Gate on `viewed_not_sold` / `dead_listings`.* Rejected — that's a taste judgment made with 5-20 lifetime views of evidence. Round-robin coverage is the honest policy when there's no signal.
- *Let the LLM rank candidates.* Rejected — that's exactly the "AI lacks taste" failure. It picks copy; SQL picks targets.
- *Do live posting first, skip the draft variant.* Rejected — see §0.

**Smallest test that proves it:** one test asserting `_candidates()` returns a never-pinned active listing, does **not** return it a second time within the cooldown window, and does not return an inactive one — plus a `materialize()` call with a hallucinated `target_id` returning `None`, and one with an over-length description returning `None` (dropped, not truncated).

**Rollback:** unregister in `cli.py` (one line). Variant B additionally: `ops undo` on each posted action deletes the pin. Events stay in the log — they're immutable, and the projection just stops being fed.

### 2.5 Amendment, 2026-08-25 (T5+E5): owned-channel IG/FB captions join Phase 1 — this section's explore policy does NOT transfer

**Premise-gate finding that produced this amendment:** ~75% of this shop's historical sales came from the operator's personal Instagram/Facebook network, not Etsy search — a fact this plan omitted when it was written. `docs/research/2026-08-24-etsy-path-to-profitability.md` §"what I need from you"/Decision Audit Trail #2 records the operator's premise-gate correction: Pinterest stays, and owned-channel `social.caption_draft` posting joins Phase 1 alongside it.

**The eligibility-policy question this raised, and why the answer here is "no, do not widen":** §2.1 above spends several paragraphs justifying an *explore* (coverage-first, no proof required) policy for Pinterest pins specifically, and is explicit that the reasoning **does not transfer** to `caption_draft`'s one-shot feed posts, because a feed post spends the shop's audience attention once and dies in a day, while a pin is free, individually deletable, one-of-dozens, and — "the load-bearing difference" — a long-lived search-index entry.

The 75%-owned-network evidence could be read as an argument to loosen `caption_draft`'s own proof requirement for IG/FB specifically, since it's the operator's *own* audience, not a cold feed. Eng review (Decision Audit Trail #9, "avoids doc contradiction and target_id collision," explicitly rejecting "copy-paste explore policy into caption_draft") and the implementation (`pipeline/ops/capabilities/caption_draft.py`'s module docstring) both land the opposite way, and the reasoning is worth stating here so this doc and the code don't quietly disagree:

- **A personal IG/FB feed post is not a Pinterest pin on the one axis that actually matters.** It is not search-indexed, does not surface for months, and dies in the feed algorithm within about a day — exactly the "one-shot feed post" profile this section's own §2.1 explicitly excludes from the explore policy, using almost identical language (`docs/research/2026-08-24-etsy-path-to-profitability.md` makes the same point independently: Pinterest is unusually good for this shop precisely *because* it's "unlike a social feed post that dies in a day").
- **The 75% figure is an argument about frequency, not about proof.** It says this channel converts better than any other the shop has, which argues for using it MORE — a real caption-drafting cadence, not an occasional afterthought — not for showing it *less-proven* inventory. The two are independent questions this doc was at risk of conflating.
- **It is arguably less forgiving of a miss than a pin, not more.** A pin competes for space on an anonymous search index; a personal feed post is seen once, by name, by real people who know the operator — a bad post there costs real social capital a bad pin never touches.

**The resolution actually shipped:** eligibility is a **per-channel config field** (`_OpsSocialChannel.eligibility`, `"explore" | "proven"`, `config/defaults/ops.json`'s `caption.channels`), not a hardcoded policy per capability — the same mechanism (`pipeline/ops/social.py`'s `mark_posted()`, plus `caption_draft.py`'s `_explore_candidates()`/`_proven_candidates()`) now backs both channels and could back a future Pinterest-caption-style channel too. Both shipped channels (`instagram`, `facebook`) default to **`"proven"`** — unchanged from `caption_draft`'s original policy — for the reasons above. `"explore"` is a real, tested value any channel can opt into later without a code change, if evidence ever argues the bar should move.

**Target identity, mark-posted, and cooldown (mechanical fallout of adding a second channel to one capability):** `target_id` became `"{listing_id}:{channel}"` so an instagram draft and a facebook draft for the same listing don't collide in the runner's `(capability, target_id)` proposal dedup. `social.pinterest_post`'s own `mark_posted()` was generalized (`pipeline/ops/social.py`) to also serve captions — a caption's drafted event stores its own `action_id` directly (no utm-embedding needed, unlike a pin's destination-URL join). Cooldown is per-channel config, keyed off drafted-OR-posted (not posted-only): a proposal the operator never approved still holds the anti-spam cooldown, the same "recently offered" property §2.1's own cooldown uses for pins.

---

## 3. Prioritized roadmap to actually close the loop

The loop is **propose → post → observe → adjust**. Today the shop has propose and (manually) post. **Observe is entirely missing, and that's the only thing standing between "an agent running a business" and "an agent broadcasting into the void."**

### P0 — Pin-draft variant A + the UTM destination URL (no adapter, no sign-off)
Starts producing the input to the loop this week. The UTM `utm_content={action_id}` must ship in P0 even though nothing reads it yet — pins are long-lived, and pins posted without it are permanently unattributable.

### P1 — Outcome projection + experiment readout (no adapter, no new service)
`proj_pin_experiments`: one row per proposed pin (`action_id`, `listing_id`, `posted_at`) joined against the already-collected `proj_listing_daily.views` delta in the N days after posting, versus that listing's own prior baseline. Plus `analytics.pin_experiment_readout()` — pure SQL, no LLM.

**Highest-value item on the list.** Honest ceiling: correlational, not attribution — cannot separate a pin from an Etsy-search impression. Workable at this catalog's near-zero baseline traffic because a listing going 2 views/week → 15 is not ambiguous.

### P2 — Pinterest read adapter, live (blocked on the Pinterest credential)
`pin_analytics()` gives outbound clicks per pin. Read-only scopes mean the first live Pinterest credential cannot post anything — ranks above live posting because reading analytics for 30+ pins by hand doesn't scale the way posting 5/week does.

### P3 — Pinterest write adapter, live (blocked on the credential + the policy verdict)
Justified only once P0 shows the copy is good and P2 shows pins get clicks. Requires the Pinterest policy verdict from §2.4 first.

### P4 — Etsy `feedback_r` scope + review ingestion — **DONE** (shipped `df8e20f`, `cfda2b4`)
Review velocity is a named Etsy ranking input. Cheap, read-only, second real outcome variable. The scope was granted on the existing Etsy credential and review reads are live; this line is kept for the record.

### P5 — Meta `live.py`
Deprioritize, don't cancel. App Review + Business Verification is real operator paperwork for a channel research puts below Pinterest for wall art, with posts that die in 24h (worse experiment substrate than Pinterest's months-long pin lifespan).

### P6 — Image-generation adapter
Phase 3 in the profitability doc. Flag: this is the one item where "AI never touches the photograph" needs an explicit written boundary before any code — the adapter interface should structurally forbid image input (text-prompt-only signature), enforced by the type system, not discipline.

### Not on the list, deliberately
- **Etsy Ads / coupons / buyer messaging** — PROHIBITED per the existing policy doc (E9/E10/E11).
- **Etsy traffic-source stats** — Shop Stats is dashboard-only; no v3 endpoint exists.
- **A scheduler** — `ops run` on the existing cron already is one.

---

## 4. Register: what needs the operator, and what doesn't

**Amended 2026-08-25.** This section was originally a blanket sign-off register that marked items 4-9 "Not approved" and said none of them could be started. That framing predates — by one day — the governance rewrite in `CLAUDE.md` ("Governance & decision authority", explicit operator authorization, 2026-08-24), under which **architecture changes, adapter interface changes, ordinary dependencies, AI model/provider selection, and the addition of external services are normal delegated implementation decisions, not approval gates.** Human review is required only at the named Operator Review Boundaries: financial, security, destructive-data, legal/platform, business-identity, budget-expansion, truly-irreversible-high-impact.

Leaving the old table standing would have created artificial blocking and let a roadmap's status column substitute for business judgment. It is reclassified below against the actual boundaries. Rows 1-3 are unchanged; the original "Not approved" wording for 4-9 is preserved in the Original column so the record of what was decided on 2026-08-24 stays legible.

| # | Item | Real boundary (if any) | Status | Original (2026-08-24) |
|---|---|---|---|---|
| 1 | `adapters/pinterest/` interface + models + fake | — | **Approved 2026-08-24** (would be delegated under current governance anyway) | Approved 2026-08-24 |
| 2 | `social.pinterest_post` capability, Variant A | — | **Approved 2026-08-24** | Approved 2026-08-24 |
| 3 | `_OpsPinterest` block in `ops.json` (+ `PlannerLimits` fields) | — | **Approved 2026-08-24** | Approved 2026-08-24 |
| 4 | Pinterest business account + developer app + OAuth grant + credential storage | **Security** (account ownership, auth, secrets) | **Operator action required.** Not a judgment on the design — a human has to convert the account, register the app, and click through the OAuth consent; the resulting token is a secret. Free, no recurring cost, so no financial boundary. | Not approved |
| 5 | `adapters/pinterest/live.py` (read) | — | **Delegated — build it.** Writing an httpx client against an already-settled Protocol is ordinary engineering. Cannot be *exercised* until item 4 exists; that is a missing capability (a credential), not a missing approval. | Not approved |
| 6 | `adapters/pinterest/live.py` (write) + `policy_verified = True` | **Legal/platform** for the `policy_verified` flag only | **Code delegated; the flag is gated.** The adapter and the capability wiring are ordinary work. Flipping `policy_verified = True` asserts a platform-terms verdict, so it waits on a Pinterest row in `docs/policy/2026-08-11-autonomy-platform-policy.md` (§2.4). `Tier.NOTIFY` per the header note. Also blocked on item 4's credential. | Not approved |
| 7 | Etsy `feedback_r` scope | — | **Done — this row is stale.** The scope was granted on the existing Etsy credential and review reads shipped (`df8e20f`, `cfda2b4`). No new account, no new secret, read-only. | Not approved |
| 8 | Image-generation adapter + provider choice | — | **Delegated.** Provider selection and dependency addition are explicitly named non-boundaries. Two live constraints, neither an approval gate: the "AI never touches the photograph" guardrail (enforce structurally — text-prompt-only signature, per P6), and any paid tier has to fit inside the existing $20/month cap. A provider that would push past that cap is a Budget-expansion escalation, not a design question. | Not approved |
| 9 | Meta App Review / Business Verification | **Business-identity** (verification submits business documents and identity) + **Security** (app credentials) | **Operator action required** for the verification paperwork itself. Deprioritized regardless — see P5. `live.py` against the existing `adapters/meta` interface stays delegated. | Not approved |

Net: the only things genuinely waiting on a human are a Pinterest account/credential (item 4), Meta's verification paperwork (item 9), and a written Pinterest policy verdict before `policy_verified = True` (item 6). Items 5 and 8 are ordinary engineering and can proceed; item 7 already shipped.
