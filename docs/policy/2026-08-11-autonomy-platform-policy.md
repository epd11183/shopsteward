# Autonomy Platform-Policy Verification — Etsy & Meta

**Written:** 2026-08-11. **Sources accessed:** 2026-08-10 (live).
**Purpose:** Resolve the M8a hard prerequisite (handoff §"verify platform policy
FIRST"; design draft §0(b) questions P1–P10). For every proposed autonomous
revenue action, a verdict — **PERMITTED / RESTRICTED / PROHIBITED** — against
Etsy's and Meta's own current API references and policies, with citations.
**Anything PROHIBITED is cut from the automation scope, not worked around.**

Method: three parallel read-only research passes against
`developers.etsy.com` (API reference + tutorials), `etsy.com/legal`
(Seller Policy, API Terms of Use), and `developers.facebook.com/docs`
(Instagram Platform, Pages API, Marketing API, Access Levels). Verbatim quotes
retained. Where no purpose-specific text was found, the point is marked
**UNVERIFIED** rather than inferred.

Etsy scopes held today: `listings_r listings_w transactions_r shops_r`.
Meta assets assumed: one IG Business/Creator account linked to a FB Page, both
owned by the operator's Business.

---

## 1. Verdict summary

### Etsy

| # | Action | Verdict | API mechanism | Scope | Governing basis |
|---|---|---|---|---|---|
| E1 | **Auto-renew toggle** (`should_auto_renew` on/off) | **PERMITTED** | `updateListing` PATCH | `listings_w` (already held) | First-class paid feature; no restricting clause |
| E2 | **Reprice a live listing** | **PERMITTED** | `updateListingInventory` PUT (`offerings[].price`) | `listings_w` | Seller Policy §1.d.6 honesty only; no cadence limit |
| E3 | **Re-tag / SEO edit** (title/tags/description) | **PERMITTED** | `updateListing` PATCH | `listings_w` | §1.c.4 accuracy only |
| E4 | **Deactivate / pause** (state→inactive) | **PERMITTED** | `updateListing` PATCH (`state`) | `listings_w` | Normal shop management |
| E5 | **Gap-fill draft** (new format / restock / bundle) | **PERMITTED** | `createDraftListing` → Gate 3 | `listings_w` | Ends in an unpublished draft; Gate 3 is the approval |
| E6 | **Bulk edits** (E3/E4 portfolio-scale) | **PERMITTED, rate-limited** | per-listing calls; **no bulk endpoint** | `listings_w` | 10 QPS / 10k QPD; §1.c.7 at extreme cadence |
| E7 | **A/B copy on live listings** | **RESTRICTED** | = E3 mechanically | `listings_w` | Mechanically fine; design §14 non-goal — resets listing history, correlational without M7 |
| E8 | **Relist-churn for search recency** | **PROHIBITED (at scale)** | **no renew endpoint exists**; synthesized deactivate→reactivate / delete+recreate | `listings_w` (+`listings_d` for delete) | §1.c.7 "manipulating search" — **UNVERIFIED** that recency-churn is named verbatim; treat as high-risk-by-general-clause |
| E9 | **Coupons / sales** | **PROHIBITED (no mechanism)** | none in v3 | — | Dashboard-only; API Terms §5(1) bars circumventing checkout |
| E10 | **Etsy Ads** (on/off, budget, stats) | **PROHIBITED (no mechanism)** | none in v3 | — | Dashboard-only; §5(15) bars connecting to ad/marketing platforms |
| E11 | **Buyer messaging** (read/send Convos) | **PROHIBITED** | none in v3 | — | §5(15)–(16) affirmatively bar machine-sent marketing/order messages to buyers |
| E12 | **Review solicitation** | **RESTRICTED** | no API channel | — | Manual honest ask OK; **never** incentivize/gate/automate (Seller Policy §3a(3–4), API Terms §5(21)) |
| E13 | **Respond to a public review** | **RESTRICTED (dashboard-only)** | **read-only** API (`getReviewsByShop/ByListing`) | `feedback_r` to read | Responding allowed as a seller act, but **no API to post** it |
| E14 | **Refunds / disputes / cases** | **PROHIBITED (no mechanism)** | none in v3 | — | Manual Shop-Manager/Payment-account process (Seller Policy §3b) |

### Meta (Instagram + Facebook)

| # | Action | Verdict | API mechanism | Access gate |
|---|---|---|---|---|
| M1 | **Publish IG feed post** (image+caption) | **PERMITTED** | `POST /{ig}/media` → `/media_publish` | App Review + Business Verification; **100 posts / 24h** |
| M2 | **Cross-post to linked FB Page** | **PERMITTED** | `POST /{page}/feed` or `/photos` | Same review bundle (owned Page → no extra tier) |
| M3 | **Reply to IG/FB comments or DMs** | **PERMITTED (windowed)** | comment `replies`; messaging `messages` | 24h response window; **no cold-DM**; 7-day private-reply window |
| M4 | **Meta paid ads** | **PERMITTED — full project** | Marketing API (`/campaigns` `/adsets` `/ads`) | `ads_management` App Review + **Business Verification (hard req)** + Full-tier 500-call/15-day qualification |

---

## 2. What this means for the build

### Cut from automation entirely (PROHIBITED)
**E9 coupons/sales, E10 Etsy Ads, E11 buyer messaging, E14 refunds/disputes,
E13 review responses (no write API), and E8 relist-churn-for-recency.** Five of
these have **no API endpoint at all** — no scope request unlocks them because
the endpoints don't exist. Buyer messaging is *additionally* barred by API
Terms §5(15)–(16). These become **operator-manual, dashboard-only** steps; the
Brief may *surface* them ("consider a sale on X") but the tool must never drive
them. This directly confirms design §3.2's T3 wall and §14 non-goals — now on
evidence, not caution.

### Permitted, build behind the governor + Gate-3 (the real M8 action surface)
**E1 auto-renew, E2 reprice, E3 SEO edit, E4 deactivate, E5 gap-fill draft.**
All within the `listings_w` scope already consented — **no new Etsy scope, no
re-auth round for any M8a/M8b listing action.** Per the app's model, anything
customer-visible (E2/E3/E4) terminates in a **Gate-3 draft or a T2 proposal**,
never an unattended live write; E5 is already a draft and Gate 3 owns it.

### E1 auto-renew is the correct first write — confirmed on policy
`listings_w` only, no `billing_r` needed to set the flag, reversible for months
before expiry, touches no customer, and Etsy treats it as a sanctioned feature.
Matches design §7's choice.

### Defer (PERMITTED but gated on external onboarding)
**M1–M3 (IG/FB promotion)** are permitted but require one **App Review +
Business Verification** pass before *any* production use — even on owned assets.
Correction to the draft: the IG content-publishing limit is **100 posts/24h**,
not 25/50. **M4 (paid ads)** is a heavier separate project (verification is a
hard requirement plus a 500-call qualification). Meta stays out of M8a; wire the
dead `adapters/meta` only after the review/verification clears (design §9 slice 8).

### Standing Etsy obligations (apply regardless of action)
- **Application Purpose approval** is required even for personal/own-shop use
  (API Terms §3) — a registration step, not a code step; flag to operator.
- **Dormant-app rule:** no successful call in 6 consecutive months lets Etsy
  suspend the key (§3). A scheduled read-sync keeps it warm.
- **Honesty/accuracy:** pricing (§1.d.6) and listing representation (§1.c.4)
  must stay truthful — a constraint on *what* an SEO/reprice action may emit.

---

## 3. Citations

**Etsy**
- API reference & listing lifecycle / scopes: https://developer.etsy.com/documentation/tutorials/listings ; https://developers.etsy.com/documentation/reference/
- No renew endpoint in v3 (staff confirmation): https://github.com/etsy/open-api/discussions/690
- Etsy Ads not in API (open feature request): https://github.com/etsy/open-api/discussions/1082 ; https://github.com/etsy/open-api/discussions/730
- Review-ID / no response endpoint: https://github.com/etsy/open-api/discussions/1076
- Rate limits (10 QPS / 10k QPD, 429 + retry-after): https://developer.etsy.com/documentation/essentials/rate-limits/
- API Terms of Use (§1 license, §3 Application Purpose + dormant-app, §5 prohibited behavior incl. §5(1),(13),(15),(16),(21)): https://www.etsy.com/legal/api/
- Seller Policy (§1.c.4 accuracy, §1.c.7 search manipulation, §1.d.6 honest pricing, §3a Reviews, §3b Case System): https://www.etsy.com/legal/sellers/
- Sales & Coupons are a manual tool: https://help.etsy.com/hc/en-us/articles/115014260108

**Meta**
- IG Content Publishing (endpoints + 100/24h limit): https://developers.facebook.com/docs/instagram-platform/content-publishing/
- Pages API posts: https://developers.facebook.com/docs/pages-api/posts/
- Permissions reference: https://developers.facebook.com/docs/permissions/
- IG comment moderation / private replies (windows): https://developers.facebook.com/docs/instagram-platform/comment-moderation/ ; https://developers.facebook.com/docs/instagram-platform/private-replies/
- Marketing API authorization + access levels (Business Verification hard req, Full-tier qualification): https://developers.facebook.com/docs/marketing-api/get-started/authorization ; https://developers.facebook.com/docs/graph-api/overview/access-levels/

---

## 4. Open / UNVERIFIED

- **E8 recency-churn:** No Etsy clause names "renewing/relisting to refresh
  recency" verbatim; the prohibition rests on the general §1.c.7 "manipulating
  search" language. Treated as **high-risk-by-general-clause and cut from
  automation.** The design never needed it (there is no renew endpoint anyway).
- **E13 read scope nuance:** `getReviewsByListing` may be API-key-only while
  `getReviewsByShop`'s buyer fields need `feedback_r`; immaterial to M8a (we
  don't read reviews yet).

**Bottom line:** the autonomous *action* surface Etsy permits is exactly
listing-lifecycle writes under `listings_w` (auto-renew, reprice, SEO edit,
deactivate, gap-fill draft). Everything customer-facing — messages, reviews,
refunds, coupons, ads — is either absent from the API or affirmatively barred,
and is operator-manual. Meta promotion is permitted but gated behind one
App-Review/Business-Verification onboarding and stays out of M8a.
</content>
</invoke>
