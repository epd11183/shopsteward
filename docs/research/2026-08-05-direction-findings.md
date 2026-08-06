# Direction findings — 2026-08-05

*What the tool survey and the shop data imply for direction, as distinct from
what they imply for implementation. Implementation detail lives in
`2026-08-05-external-tools-survey.md`; this file is about what we should
reconsider.*

**Three of these contradict PRD v2.1.** Per CLAUDE.md the PRD wins and the
discrepancy gets flagged — flagging it is what this document is. Nothing here
has been acted on.

---

## Decisions this puts in front of you

| # | Decision | If you don't decide |
|---|---|---|
| 1 | Does Pinterest move ahead of Instagram, against PRD §3's explicit deferral? | M6 builds Instagram, and the evidence says it won't move traffic |
| 2 | Do operators get a "bring your own PSD template" path alongside the shipped library? | Shipped AI-generated library only; we hand-annotate every wall region |
| 3 | Does anything demand-side enter the roadmap before M8? | Roadmap stays entirely supply-side |
| 4 | Per-product resolution floors, with acrylic **strictest**? | One global floor, and the premium SKU is the one that ships soft |

Only #1 is urgent, and only because M6 is a single weekend that could be spent
on the wrong platform.

---

## 1. The social stage targets the wrong platform — **PRD discrepancy**

**What the PRD says.** Two places, and they reinforce each other:

- **§2 goals:** *"Enforce an Instagram cadence of at least 4 posts per week with
  no more than one operator tap per post."* A success metric, not a nice-to-have.
- **§3 non-goals:** *"Channels beyond Etsy and Instagram (Pinterest, Facebook,
  TikTok deferred to v2)."* **Pinterest is explicitly out of scope.**
- **§10:** M6 is *"Instagram asset generation + scheduled posting, 1 weekend."*

**What the evidence says.**

1. **Instagram stills don't acquire.** Converging sources: Reels drive discovery;
   static feed posts reach existing followers. Our pipeline produces a single
   composited photograph. A 4-posts-per-week photo cadence is *notifying people
   who already follow us*, which is not the goal §2 thinks it is measuring.
2. **Pinterest is already live and producing, with zero code.** The shop is
   claimed, Rich Pins are on, and a milestone email on 2026-08-05 reports **one
   pin at 250 impressions — the black bear cub**, the single subject that has
   sold at both ends of the ladder ($7.99 download, $107.67 canvas). The shop
   has **1,754 lifetime Etsy views**. One pin is earning reach faster than the
   listing page ever has.
3. **Pinterest is the better automation target regardless.** API v5, free at
   Trial and Standard, 100 writes/min, and pins are evergreen search objects
   rather than feed items with a half-life in hours.
4. **The unofficial Instagram path is closed anyway.** `instagrapi`'s own README
   steers business users to the official API; reported suspension rates are
   15–30%/yr for app-emulating automation vs <0.5% for official. The official
   API needs app review (2–4 weeks, one-time) and is JPEG-only.

**What it changes.** §3 defers Pinterest to v2 on the assumption that Instagram
is the primary channel and Pinterest is a later broadening. The data inverts the
premise: Pinterest is the channel that is already working. **M6 should probably
be Pinterest, with Instagram deferred** — the exact reverse of the current text.

⚠ **Impressions are not clicks.** A milestone email is engagement bait fired at
a round number. **The decisive number is Etsy Stats → Traffic Sources**
(TODO #6), which the Etsy API does not expose and which must be read by hand.
Read that before reordering anything.

**What not to do.** Don't build both. Don't build either before reading the
traffic-source data. And if Pinterest turns out to be negligible *despite* pins
earning impressions, that is also an answer — it means pins get seen but don't
convert, and the fix is pin copy and destination, not an adapter.

---

## 2. The mockup template source — **a refinement, and a correction**

**I initially got this wrong and the PRD is more right than I gave it credit
for.** Recording the correction because the reasoning is the useful part.

**What the PRD says (§5.3).** ~15 empty-room templates at v1, ≈3 orientations ×
5 room/style combos, **generated offline with consumer image tools** (Gemini /
ChatGPT), shipped in `config/defaults/`, extensible per operator. M4 adds "AI
template expansion."

**What I first concluded.** `psd-tools` (MIT) exposes
`layer.smart_object.transform_box` — the four corner coordinates of a smart
object's placement inside a PSD. Since the entire commercial mockup industry
runs on professional room-scene PSDs, we could buy one, read its geometry, and
`warpPerspective` ourselves — skipping both Photoshop and the SaaS paywall. I
framed this as cheaper than the PRD's approach.

**Why that was wrong on two counts.**

- **The PRD's generation is already offline and one-time.** Templates are made
  once with consumer tools and committed. There is no vision model in the render
  loop, so there was no per-render cost to save.
- **Purchased PSDs are not redistributable, and this repo is going public.**
  Creative Market and equivalent licences do not permit shipping the template in
  an open-source repo. The PRD's self-generated library is shippable precisely
  *because* we made it. **The open-source decision makes the PRD's choice more
  correct, not less.**

**What survives, and it is still worth having.** The two approaches differ in
where wall geometry comes from:

| | Shipped AI-generated library (PRD) | Operator's own PSD |
|---|---|---|
| Redistributable | **yes** — we made it | no |
| Wall geometry | we must find or annotate it | **free**, via `transform_box` |
| Quality ceiling | consumer image tools | professional photography |

**Recommendation: keep the PRD's library as the shipped default, and add
`psd-tools` as a per-operator extension path** — "bring your own PSD template."
We ship the *reader*, never the templates. §5.3 already says the library is
"extensible per-operator"; this is what that extension should look like, and it
costs one MIT dependency.

**Unchanged and worth restating:** deterministic compositing reaches
professional quality via **displacement map + Multiply blend** (`cv2.remap` plus
a numpy multiply, both already in the stack). The "no generative fill" rule
costs us nothing in realism. That was the open worry about M4 and it is closed.

---

## 3. The roadmap is entirely supply-side

**Not a PRD discrepancy — an observation about what the milestone table adds up
to.** M2 editing, M3 hero scoring, M4 mockups, M5 listings, M8 autonomy: every
one makes listings cheaper and faster to produce. §2's headline goal is
*"reduce per-listing manual effort by at least 75%."*

**But effort per listing is not the binding constraint.** The shop converts at
**2.1%, up from 0.39%** — healthy. It has **1,754 lifetime views**. We are
efficient at converting a trickle.

**More listings do buy more Etsy search surface**, so the roadmap is not
disconnected from traffic — that is the honest counter-argument and it is a real
one. But it is indirect, and the survey closed the direct lever:

- **Etsy has no search-volume or query-analytics endpoint**, and never has —
  a standing, repeatedly-requested, unfulfilled feature request on
  `etsy/open-api`. Every commercial SEO tool (eRank, Alura, Marmalead) scrapes
  Etsy's front end. **We should not build or adopt a scraper**: ToS-grey,
  fragile, and a maintenance burden a single operator should not own.
- So the only instrumentable, automatable demand channel is **off-Etsy** — and
  the one we have is already outperforming the listing page on reach.

**What this does *not* mean.** It does not mean stop building the pipeline.
TODO #1 (canvas templates) unlocks 77% of revenue and is unambiguously the right
next thing. Automation compounds and the shop is small enough that supply
capacity will matter later.

**What it does mean.** Between M4 mockups and a social adapter, the evidence now
favours the adapter, and that ordering was not obvious before today. Worth one
deliberate decision rather than defaulting to milestone order.

---

## 4. Acrylic is the least forgiving SKU, not the most

**Inverts a working assumption.** Acrylic is our premium product, so the
intuition was that it is the safest place for a marginal file. The opposite is
true: **gloss and depth reveal pixel grain that canvas texture masks.** The
mechanism is surface finish, not viewing distance.

| Source | Number |
|---|---|
| **Gelato (official)** | **150–225 PPI inside the BleedBox** — our fulfiller, the literal target |
| WhiteWall (pro lab) | 300 PPI close-viewed; **200 PPI canvas/acrylic** at ~2m |
| CanvasPop / CanvasDiscount | 150 fine for canvas; **acrylic 150–200 minimum** |

**Consequence for the viability gate:** it must be **per-product**, and acrylic
gets the *strictest* floor despite being the most expensive. Implement as a
formula rather than a lookup table — max useful PPI ≈ **6878 ÷ viewing distance
in inches** — so it generalises when a SKU is added. Suggested floors: 150
canvas/poster, 200 acrylic, 300 as the no-warning ceiling.

⚠ **Unverified:** whether Gelato re-sharpens on their end. No doc found either
way. Do not assume they cancel our sharpening — the canvas sample order
(TODO #2) is the only way to find out.

---

## 5. Two permanent data ceilings

Neither is a bug or a gap to close. Both should be written into any design that
would otherwise assume its way past them.

**Etsy gives lifetime cumulative views only.** No favourites, no visits, no time
windows, no history, no traffic sources. Confirmed across `etsy/open-api`
discussions #1304, #1386, #681 — years-old, unresolved. Every tool showing
"views this week" is polling and snapshotting client-side.

- **This validates our architecture.** Event-sourced SQLite with
  `proj_listing_daily` preserving history is not a workaround; it is the only
  known design. Stop looking for a better one.
- **But it caps §6's feedback loop.** We can attribute *sales* to photographs.
  We can never attribute *traffic*. A tuning profile that wanted traffic
  attribution cannot have it, and TODO #6's traffic-source read can never be
  automated into the shop brief.

**Per-order personalised digital files cannot be delivered via the Etsy API.**
Confirmed via `etsy/open-api` #1301, with Etsy contributors stating there is no
programmatic path into Etsy Chat. Sellers upload by hand or email. **A platform
wall, not a tooling gap** — no open-source project solves it because it is
unsolvable from outside. Any future personalisation feature is dead on arrival.

---

## 6. The gaps are the reason to publish

Three independent searches — wall-art mockups, POD print-file preparation, and
Etsy + POD end to end — returned **no mature open-source project at all.** The
Etsy client library ecosystem is a graveyard: everything v2-era died when Etsy
shut v2 down in 2022, and the one maintained MIT client is 11 stars.

That is not a gap in the searching. **The parts of this repo with no prior art
are the parts worth leading with when it goes public** — the POD-first-then-
enrich listing pattern, the deterministic print-file path, and the three-gate
operating model. The Etsy adapter is the least novel thing here, and it is the
thing that looks most like the obvious headline.

---

## PRD discrepancy register

Flagged per CLAUDE.md. **The PRD wins until you decide otherwise.**

| PRD | Says | Challenged by |
|---|---|---|
| §3 non-goals | Pinterest deferred to v2 | §1 above — Pinterest is the live channel |
| §2 goals | Instagram cadence ≥4 posts/week, one tap | §1 — stills don't drive discovery |
| §10, M6 | M6 = Instagram, 1 weekend | §1 — likely should be Pinterest |
| §2, §5, §10 | Printful named as a supplier throughout | Settled 2026-08-04: Printful's API cannot create products. Gelato is sole supplier. **PRD text is stale, not wrong-in-principle** |
| §5.3 | ~15 AI-generated empty-room templates | §2 above — **upheld**, with a BYO-PSD extension proposed |

---

## What not to re-open

- **Printful.** Enumerated four times. 89 endpoints, zero product creation.
- **Etsy SEO tooling.** No search-volume API exists to build on.
- **A better Etsy analytics source.** There isn't one.
- **Instagram via unofficial libraries.** Their maintainers say don't.
- **AI upscaling to rescue a low-resolution file.** Real-ESRGAN is BSD-3 so a
  licence-only screen would pass it; our own rule blocks it. Raise the
  resolution gate instead.
