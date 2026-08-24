# Design: Pinterest adapter + `social.pinterest_post` + loop-closing roadmap

**Author:** `architect` sub-agent, 2026-08-24. **Status:** approved by operator 2026-08-24 ("do it") for items 1-3 of the sign-off register below (interface/models/fake, the capability, config additions — all no-external-account-required). Items 4-9 (live Pinterest account/OAuth, Etsy `feedback_r` scope, image-gen adapter, Meta activation) remain NOT approved and must not be started on the strength of this document.

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

### 1.4 What the operator has to obtain (NOT approved yet — do not build)

Before `live.py` is written:

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

**Guardrail impact:** none negative. New external service (item 4 in the sign-off register). Read-only-until-approved holds: `fake.py` is the default, `live.py` doesn't exist. No dependency added — `live.py` would use `httpx`, already in-tree.

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

**Variant B — `social.pinterest_post` (live posting, NOT approved yet — requires a live Pinterest credential).** Holds a `PinterestWriteAdapter` injected at construction. `execute()` calls `create_pin`, appends `social.pin_posted`. `undo()` calls `delete_pin` and appends `social.pin_deleted`. Registered in `cli.py` only under `--live-autonomy`. **`max_tier = Tier.NOTIFY`** per the 2026-08-24 operator autonomy direction (see header note) — a pin publishes automatically and the operator is notified after, rather than requiring a click before each one, reflecting that a pin is free, deletable, and doesn't touch Etsy's search-ranking history the way a title/tag edit does.

### 2.4 `policy_verified` is currently unanswerable

Every live capability sets `policy_verified = True` on the strength of a specific verdict row in `docs/policy/2026-08-11-autonomy-platform-policy.md`. **That document covers Etsy and Meta only — there is no Pinterest section.** Variant B must not set `policy_verified = True` until a Pinterest row exists (API terms + the "no repetitive/spam pinning of the same destination" community guideline, which the cooldown is designed against). Variant A needs nothing — it makes no Pinterest call at all.

**Rejected alternatives**

- *Gate on `top_sellers()` for consistency with its siblings.* Rejected — inert at $0 revenue.
- *Gate on `viewed_not_sold` / `dead_listings`.* Rejected — that's a taste judgment made with 5-20 lifetime views of evidence. Round-robin coverage is the honest policy when there's no signal.
- *Let the LLM rank candidates.* Rejected — that's exactly the "AI lacks taste" failure. It picks copy; SQL picks targets.
- *Do live posting first, skip the draft variant.* Rejected — see §0.

**Smallest test that proves it:** one test asserting `_candidates()` returns a never-pinned active listing, does **not** return it a second time within the cooldown window, and does not return an inactive one — plus a `materialize()` call with a hallucinated `target_id` returning `None`, and one with an over-length description returning `None` (dropped, not truncated).

**Rollback:** unregister in `cli.py` (one line). Variant B additionally: `ops undo` on each posted action deletes the pin. Events stay in the log — they're immutable, and the projection just stops being fed.

---

## 3. Prioritized roadmap to actually close the loop

The loop is **propose → post → observe → adjust**. Today the shop has propose and (manually) post. **Observe is entirely missing, and that's the only thing standing between "an agent running a business" and "an agent broadcasting into the void."**

### P0 — Pin-draft variant A + the UTM destination URL (no adapter, no sign-off)
Starts producing the input to the loop this week. The UTM `utm_content={action_id}` must ship in P0 even though nothing reads it yet — pins are long-lived, and pins posted without it are permanently unattributable.

### P1 — Outcome projection + experiment readout (no adapter, no new service)
`proj_pin_experiments`: one row per proposed pin (`action_id`, `listing_id`, `posted_at`) joined against the already-collected `proj_listing_daily.views` delta in the N days after posting, versus that listing's own prior baseline. Plus `analytics.pin_experiment_readout()` — pure SQL, no LLM.

**Highest-value item on the list.** Honest ceiling: correlational, not attribution — cannot separate a pin from an Etsy-search impression. Workable at this catalog's near-zero baseline traffic because a listing going 2 views/week → 15 is not ambiguous.

### P2 — Pinterest read adapter, live (new external service — NOT approved)
`pin_analytics()` gives outbound clicks per pin. Read-only scopes mean the first live Pinterest credential cannot post anything — ranks above live posting because reading analytics for 30+ pins by hand doesn't scale the way posting 5/week does.

### P3 — Pinterest write adapter, live (new external service, write scope — NOT approved)
Justified only once P0 shows the copy is good and P2 shows pins get clicks. Requires the Pinterest policy verdict from §2.4 first.

### P4 — Etsy `feedback_r` scope + review ingestion (new scope — NOT approved)
Review velocity is a named Etsy ranking input. Cheap, read-only, second real outcome variable.

### P5 — Meta `live.py`
Deprioritize, don't cancel. App Review + Business Verification is real operator paperwork for a channel research puts below Pinterest for wall art, with posts that die in 24h (worse experiment substrate than Pinterest's months-long pin lifespan).

### P6 — Image-generation adapter
Phase 3 in the profitability doc. Flag: this is the one item where "AI never touches the photograph" needs an explicit written boundary before any code — the adapter interface should structurally forbid image input (text-prompt-only signature), enforced by the type system, not discipline.

### Not on the list, deliberately
- **Etsy Ads / coupons / buyer messaging** — PROHIBITED per the existing policy doc (E9/E10/E11).
- **Etsy traffic-source stats** — Shop Stats is dashboard-only; no v3 endpoint exists.
- **A scheduler** — `ops run` on the existing cron already is one.

---

## 4. Operator sign-off register

| # | Item | Category | Status |
|---|---|---|---|
| 1 | `adapters/pinterest/` interface + models + fake | Adapter interface addition | **Approved 2026-08-24** |
| 2 | `social.pinterest_post` capability, Variant A | New capability on the chassis | **Approved 2026-08-24** |
| 3 | `_OpsPinterest` block in `ops.json` (+ `PlannerLimits` fields) | Config schema change | **Approved 2026-08-24** |
| 4 | Pinterest as a live service — account, developer app, OAuth scopes | New external service + secrets | Not approved |
| 5 | `adapters/pinterest/live.py` (read) | New live adapter | Not approved |
| 6 | `adapters/pinterest/live.py` (write) + `policy_verified = True` | New live write path; needs a Pinterest policy row first | Not approved |
| 7 | Etsy `feedback_r` scope | New OAuth scope | Not approved |
| 8 | Image-generation adapter + provider choice | New external service + AI provider selection | Not approved |
| 9 | Meta App Review / Business Verification | New external service activation | Not approved |

Nothing in items 4-9 should be started on the strength of this document.
