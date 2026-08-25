# Design: `listing.catalog_expand` — paced digital listings from the photo archive

**Author:** `architect` sub-agent, 2026-08-25. **Status:** approved for
implementation 2026-08-25 (delegated engineering decision under the
2026-08-24 governance rewrite; the *business* decision was the operator's own
`/autoplan` gate answer — T11, verbatim: *"a new capability proposing paced
digital listings from your archive, Gate-3 approval per listing, fees within
the $20 cap."*). Scope is exactly that and nothing more.

## 0. The headline

**Nothing new needs to be built for the listing itself.** The M5a chain
already does per-photo work end to end, and both of its stages already take a
`photo_id` filter. The whole capability is:

```
adopt.ingest_one_file(path)   ->  landing row + photo_id
archive.archive_master(...)   ->  reprintable later, free
run_mockups(photo_id=...)     ->  listing images
build_drafts(photo_id=...)    ->  draft built AND pushed to Etsy as a DRAFT
                                  -> appears in gate3.queue(), operator publishes
```

Four existing calls. The capability file is thin glue plus a grounding
function. **No refactor of `drafts.py` is warranted** — the source-agnostic
entry point such a refactor would create already exists in the form of the
`photo_id` parameter both stages accept.

The real design work is (a) the eligibility bar, (b) pacing that survives the
T9 governor revert, and (c) three small chassis integrations that are easy to
get wrong and expensive when wrong.

---

## 1. What already exists (file:line)

| Question | Answer | Where |
|---|---|---|
| Does the build path require a landing-file row? | **Yes, hard.** Only `proj_landing_files WHERE status='valid'`. | `pipeline/listings/drafts.py:42-57`, `:156` |
| Does it require a completed mockup set? | **Yes, hard, for digital too** — `if mockup_set is None: continue`. | `drafts.py:71-82`, `:158-160` |
| Why do digital downloads need mockups? | The mockup set **is** the source of listing images. There is no other image source in the module. | `drafts.py:222-223` -> `images.order_listing_images` |
| Can mockups run with no template library? | Yes. Template-backed intents are skipped as `intents_skipped_no_template`; synthetic intents (`canvas_edge`, `acrylic`, `digital_whatyougot`) are pure Pillow/OpenCV and always render. A digital listing can complete on synthetic intents alone. | `mockups/jobs.py:27`; `mockups/intents.py` |
| Is per-photo scoping available? | Yes, both stages: `run_mockups(..., photo_id=...)` and `build_drafts(..., photo_id=...)`, through the *same* `_eligible_landing_rows` predicate. | `jobs.py:36-51`, `drafts.py:42-57` |
| How does an archive file legally become a landing file? | Already solved: `adopt._ingest_matched_file()` registers **exactly one** file, deliberately not calling `scan_landing()` — its docstring is the precedent for "the landing folder is the only Etsy handoff." | `pipeline/listings/adopt.py:147-220` |
| What identifies an archive photo with **no** listing? | **Nothing does today.** `proj_asset_store` is populated only by `archive.archive_master`, so it contains *only* photos that already have a listing. The unlisted-archive set is a folder on disk, not a table. | `archive.py:25-88`, `source_assets.py:29-67` |
| Push scoping? | **None.** `push_drafts` is eligibility-based (`etsy_listing_id IS NULL`) across all drafts. | `push.py:53-60`, `drafts.py:260-262` |
| Closest sibling capability | `gapfill.py` — unpublished draft, `max_tier = Tier.PROPOSE`, `undo = None`, Gate 3 as approval authority. | `ops/capabilities/gapfill.py:135-192` |

**Consequence of the "nothing identifies it" row:** the candidate set comes
from a filesystem scan, not SQL. Acceptable, with precedent
(`adopt.scan_local_files`), and it keeps the "SQL picks targets, LLM picks
copy" rule intact — the picker is deterministic code that reads a directory
instead of a table.

---

## 2. Approaches

### A. Curated-folder capability over the existing chain — **CHOSEN**

One new file `ops/capabilities/catalog_expand.py`; `execute()` runs the
four-call chain. Candidates = files in an operator-curated folder that are not
yet landing files.

- **Effort:** one evening (human) / ~1-2h (CC). **Risk:** low. No change to
  `drafts.py`/`push.py`/`gate3.py` at all.
- **Pro:** the operator's act of putting a file in the folder *is* the quality
  gate — the manual-curation model `CLAUDE.md`'s "Current focus" already
  describes. Spend flows through the governor. Gate 3 unchanged.
- **Con:** candidate discovery re-hashes the folder each `propose()` run
  (bounded: hashing skipped for files already known as landing files).

### B. Source-agnostic build entry point in `drafts.py`

- **Effort:** 2-3 evenings. **Risk:** medium-high — `drafts.py` is the most
  load-bearing, most-tested module in the listings package, and its
  fill-forward rerun semantics (`drafts.py:10-17`) are subtle.
- **Con:** buys nothing; the per-photo scoping it would create is already the
  `photo_id` parameter. **Rejected.**

### C. Plain CLI, no capability

- **Con:** no spend accounting against the $20 cap, no NEEDS-YOU surfacing, no
  pacing memory; the operator asked for a capability.
- **Synthesis taken from it:** write the chain as one plain function
  `expand_one(conn, user_id, path, *, adapter, live_copy) -> str  # draft_id`.
  The capability's `execute()` is a two-line wrapper, and a CLI can call it
  directly for a manual backfill later.

---

## 3. The capability, exactly

```
key            = "listing.catalog_expand"
target_type    = "archive_photo"
target_id      = file_id  (sha256 of the file bytes -- the same id landing uses;
                           content-addressed, survives renames, and gives the
                           runner's (capability,target_id) dedup the right grain:
                           one pending proposal per photo)
max_tier       = Tier.PROPOSE          # HARD-PINNED in Python, never promotable
policy_verified= True                  # requires policy entry E16 first -- see §8
undo           = None                  # gapfill.py precedent; registry invariant 1
                                       # permits this at max_tier=PROPOSE.
                                       # Reversal = decline at Gate 3.
estimate_cost_usd = cfg.catalog_expansion.listing_fee_usd  (0.20)
params         = {"path": <absolute path>}   # NOT an LLM-chosen field
```

**Why `Tier.PROPOSE` is a Python pin, not a config default.** Same class as
`gapfill_reprint`: it creates catalog and commits real money. Etsy has no
"un-list without a trace" — a deleted listing takes its search history with
it, and every listing added dilutes a shop-level conversion signal that is
itself a ranking input. `registry.register()` (`registry.py:67-73`) plus
`runner._maybe_promote`'s `int(to_tier) < int(cap.max_tier)` check
(`runner.py:347`) mean no config path can raise it.

**One proposal = one listing.** Batch proposals break "Gate-3 approval per
listing" (the operator's own words) and defeat the runner's dedup grain.

**LLM boundary.** `propose()` is deterministic and non-empty (`renew.py`
precedent). `_candidates()` is filesystem + SQL only. `materialize()` looks the
target up in the same dict, so the planner can only *name* a photo
`_candidates()` already found — never introduce one. The LLM's entire role is
inside the existing copy stage (`copy.py`): title/tags/description with the
existing AI-disclosure line. **The LLM never selects a photograph, and nothing
in this path touches the photograph's pixels** — the only image transform in
the chain is `images._load_sellable`'s deterministic max-quality sRGB JPEG
re-encode (`images.py:36-51`): no resize, no upscale, no fill, no generative
step, ever.

**`_candidates(conn, user_id, cfg)`:**

1. `{}` if `not cfg.catalog_expansion.enabled` or the folder is missing.
2. `paths = adopt.scan_local_files(folder, recursive)` (sorted; extend
   `_JPG_SUFFIXES` to include `.tif/.tiff` — landing already allows TIFF).
3. Skip any `file_id` already in `landing._known_file_ids(conn, user_id)` —
   permanent execution idempotency for free (`adopt`'s own trick).
4. Skip any `file_id` with a prior `action.rejected` for this capability
   (`adopt._revoked` precedent, ~6 lines). **Load-bearing — see §7.**
5. Validate format + `long_edge_px >= cfg.catalog_expansion.min_long_edge_px` (§6).
6. Near-duplicate guard: pHash (`photo_match.phash_bytes`) against other
   candidates this run and already-registered landing files; both local, zero
   network. `hamming_distance <= cfg.catalog_expansion.dedup_max_distance` -> skip.
7. Pace: sort by path, take `max_new_per_week - executed_this_iso_week` (§5).

**`precondition_ok = live_copy_open()`.** The governor already checks
`getattr(cap, "precondition_ok", True)` -> `RefusalReason.PRECONDITION`
(`governor.py:220-221`). Without it, a run with the `FixtureCopyAdapter` would
put **canned fixture copy on a real Etsy listing**. Use the existing hook.

**`execute()`:** `expand_one(...)`, then read the resulting `draft_id` back
(`SELECT draft_id FROM proj_listing_drafts WHERE user_id=? AND
landing_file_id=?`) for `ExecutionResult.after`. Re-validate eligibility first
and raise if stale (`renew.py:190-194` precedent) — never spend on a decision
that changed.

**Cost accounting — the honest version.** Etsy charges the $0.20 at *publish*,
which happens at Gate 3, **outside the chassis**. `execute()` therefore does
not actually spend. We still return `cost_usd = 0.20`, a deliberate
**over-reserve**, because it is the only point the governor can see. A
declined draft is over-counted — which fails safe; under-counting would let 40
listings publish against a governor that saw $0.

**Second, uncounted spend line.** `live_copy=True` bills OpenRouter through
`llm_ledger`'s *separate* monthly soft cap
(`tuning.vision.monthly_soft_cap_usd`), not the governor's
`monthly_spend_cap_usd`. Roughly $0.01-0.03/listing, ~$0.12/mo at 5/week —
immaterial, but it is a real second meter the $20 cap does not see.

---

## 4. Chassis integrations (three, all small, all easy to get wrong)

1. **`runner.LIVE_GATED_CAPABILITIES` must include `"listing.catalog_expand"`.**
   Its `execute()` reaches a real Etsy write surface. Unlike other members of
   that set, an approve against a fresh `FakeEtsyWriteAdapter` would *succeed*
   (the fake happily creates a listing) and record a bogus `action.executed` —
   a terminal state permanently blocking the real approval. Same failure that
   burned the operator on 2026-08-24, one shade worse.
2. **Portfolio-cap exemption** in `governor.py` — see §5.
3. **Holdout: no change needed.** `_pin_event_dates` does `int(target_id)` and
   returns `[]` on `ValueError`; a `file_id` target is never int-parseable, so
   the pin/seo holdout can never fire here. Correct by construction — state it
   in the docstring so nobody "fixes" it later.

Use `timeutil.parse_ts` for the weekly-count `created_at` comparison rather
than string slicing.

---

## 5. Pacing

```json
"catalog_expansion": {
  "enabled": true,
  "source_folder": "data/catalog_candidates",
  "recursive": true,
  "max_new_per_week": 5,
  "min_long_edge_px": 6000,
  "dedup_max_distance": 6,
  "listing_fee_usd": 0.20
}
```

Enforced **inside `_candidates()`**, counting this capability's
`action.executed` rows in the current ISO week — the same history
`governor._executed_this_iso_week` already reads. Not a new governor concept.

**Interaction with the T9 revert (~2026-08-31) — the one that will silently
break it.** Today's caps are near-no-ops (`daily_action_cap` 1000,
`per_capability_daily_cap` 1000, `weekly_catalog_pct_cap` 1.0). When T9
restores real values, `governor.py:250-255` computes `projected / active >
weekly_catalog_pct_cap` where `active` is `COUNT(*) FROM proj_listings WHERE
state='active'` — **~34, not ~125**, because expired/inactive/sold_out do not
count. At a restored 0.05 that permits **one** catalog_expand execution per
week, throttling the initiative by 80% with no error message.

**Decision: exempt this capability from the portfolio cap.** That cap exists to
stop mass *churn of the existing catalog*; a capability that only ever adds is
not the risk it was designed for, and it is the only capability whose success
grows the denominator. One frozenset in `governor.py`:

```python
_PORTFOLIO_CAP_EXEMPT = frozenset({"listing.catalog_expand"})
```

**This goes on the T9 revert checklist explicitly.** The alternative (tuning
`weekly_catalog_pct_cap` upward for everyone) re-opens churn risk on the
existing catalog to solve a problem that is not churn.

Budget at the default pace: 5/wk x $0.20 ~= **$4.33/mo** of listing fees (plus
~$0.12 LLM) against a $20 cap currently running at $0.40/mo. The governor's
`BUDGET` refusal is the backstop.

**Operator attention budget:** 5 Gate-3 cards/week ~= 10 minutes. That is the
actual reason the default is 5 and not 20. Raise it only after a month of the
queue staying empty.

---

## 6. The quality gate

An archive photo is not automatically a product. The honest bar, in order:

1. **Operator pre-curation is the primary gate.** Only files in
   `data/catalog_candidates/` are considered. Putting a file there *is* the
   "this is sellable" judgment — matching `CLAUDE.md`'s current model ("the
   operator now culls winners manually").
2. **Resolution — the default here is a real finding.**
   `config/defaults/mockups.json` advertises `whatyougot.sizes` up to **16x20
   at 300 DPI**, which needs a **6000 px long edge**. The landing default is
   `min_long_edge_px: 3000` (`tuning_profile.json:17`) — a 3000 px file listed
   against that copy delivers 16x20 at 150 DPI, i.e. **the listing promises
   something the file cannot deliver.** So `catalog_expansion.min_long_edge_px`
   defaults to **6000**, not 3000. If the archive cannot clear 6000 px, the
   correct fix is to trim `whatyougot.sizes`, **not** to upscale — *AI never
   touches the photograph*: no generative upscale, no fill, no edit, ever.
   **This also implicates existing listings built under the 3000 px floor —
   see §12.7.**
3. **Format:** JPEG/TIFF per `landing.allowed_formats`; validation is
   `landing._validate`, reused, never reimplemented.
4. **Not already listed / not a near-duplicate:** sha256 exact, pHash near.
5. **No vision score.** Resurrecting a viability model here would rebuild the
   Gate 1 scoring pipeline the operator deliberately deleted 2026-08-09.
   Explicitly out of scope.

---

## 7. Rejection is the normal verdict, and the chassis does not model that

For every other capability, `ops reject` means "the agent proposed something
wrong." Here, "no, not that photo" is the *expected* routine answer.

- Rejection is **not** harmful to the ladder: at `max_tier = Tier.PROPOSE`,
  `runner._demote`'s `min(from+1, PROPOSE)` is a no-op. Only the `rejections`
  counter moves.
- But a rejected photo **comes back tomorrow**: `action_id` includes the day,
  so the next run mints a fresh id for the same file, and `_TERMINAL` is
  per-`action_id`, not per-target.

Fix: `_candidates()` skips any `file_id` with a prior `action.rejected` for
this capability (§3 step 4, `adopt._revoked` precedent, no new event type, no
new CLI verb). Moving the file out of the folder is the documented "I changed
my mind later" path.

---

## 8. Config, events, and code changes

**New config:** the `catalog_expansion` block above in
`config/defaults/ops.json` + `_OpsCatalogExpansion` in `ops/models.py`
(mirroring `_OpsRenew`; `listing_fee_usd` uses `gt=0` for the same reason
`renew` does — a zero would let `month_spend()` undercount real money).

**New event types: none.** Every step reuses existing events
(`landing.file_observed`, `asset.archived`, `mockup.generated`/
`mockupset.completed`, `listingdraft.*`, `action.*`, `gate3.*`). Zero new event
types is the strongest signal this design rides the existing rails.

| File | Change |
|---|---|
| `ops/capabilities/catalog_expand.py` | new, ~180 lines |
| `pipeline/listings/adopt.py` | rename `_ingest_matched_file` -> `ingest_one_file` (two callers); 3-line diff |
| `pipeline/ops/governor.py` | `_PORTFOLIO_CAP_EXEMPT` frozenset, checked at `:250` |
| `pipeline/ops/runner.py` | add key to `LIVE_GATED_CAPABILITIES` |
| `pipeline/ops/models.py`, `config/defaults/ops.json` | config block |
| `pipeline/ops/cli.py` | `register(ListingCatalogExpand(adapter, live_copy=live_copy_open()))` at both registration sites |
| `docs/policy/2026-08-11-autonomy-platform-policy.md` | **new entry E16** — prerequisite for `policy_verified = True` |

---

## 9. The smallest test that proves it

One test, `tests/pipeline/ops/test_catalog_expand.py`: seed a temp candidate
folder with one 6000 px JPEG and an in-memory DB with a seeded config, then:

1. `propose()` -> exactly one action; `target_id == sha256(file)`;
   `estimated_cost_usd == 0.20`; `tier == PROPOSE`.
2. `approve_action(...)` with a `FakeEtsyWriteAdapter` -> `gate3.queue()` has
   one card, `state == "pushed"`, non-null `etsy_listing_id`, non-null title
   and price.
3. `propose()` again -> `[]` (the file is now a landing file).

Plus a 4-line unit test that a 3000 px file is *not* proposed (§6's resolution
bar) — the branch most likely to be silently wrong.

---

## 10. Rollback

**Levers, cheapest first:** empty the candidate folder (zero code) ->
`catalog_expansion.enabled: false` (config, no deploy) -> `ops halt` -> remove
the `register(...)` line. Drafts pushed but unpublished are Etsy drafts: no
fee, deletable, invisible to buyers. Published listings reverse with the
existing `listing.deactivate` (fee unrecoverable — same honesty as
`renew.undo`).

**Roll back if:**
1. More than 2 of the first 10 proposals are photos the operator would not list
   -> the eligibility bar is wrong, not the pacing.
2. The Gate-3 queue holds >10 unreviewed cards for >14 days -> attention cost
   exceeded; drop `max_new_per_week` before disabling.
3. Shop-level conversion rate or search impressions decline over a 30-day
   window following +20 listings -> quality-signal dilution is real.
4. Any Etsy policy notice about duplicate/similar listings.

---

## 11. Rejected alternatives

- **Refactor `drafts.py` into a source-agnostic build entry point** — the
  `photo_id` parameter already is one.
- **A parallel archive->listing pipeline** — would duplicate copy, pricing,
  images, push, and Gate 3. Non-starter.
- **Vision-scored auto-curation** — rebuilds the deleted Gate 1 pipeline.
- **Batch proposals** — breaks per-listing Gate 3 approval and the dedup grain.
- **POD/physical expansion first** — ~$25 capital at risk per canvas unit vs
  $0.20 for digital; `gapfill_reprint` already covers proven winners.
- **Generative upscaling of sub-6000 px archive files** — prohibited outright.
- **Raising `weekly_catalog_pct_cap` globally instead of exempting** —
  re-opens churn risk to solve a problem that is not churn.

---

## 12. Risks the gate may not have priced

1. **Shop-quality dilution.** At ~34 active listings, adding 20 low-traffic
   listings can *lower* shop-level conversion rate, itself an Etsy ranking
   input. More listings is a traffic *bet*, not a certainty. Pacing at 5/week
   is the hedge; rollback criterion 3 is the tripwire.
2. **Near-duplicate cannibalization pHash cannot catch.** pHash blocks the same
   frame twice, not twenty different frames of the same lighthouse competing
   with each other in Etsy search. Operator curation is the only real defense.
3. **`push_drafts` scope leak.** `build_drafts` ends by pushing *every*
   fully-built unpushed draft, not just this photo's. Blast radius is an
   unpublished Etsy draft (no fee, Gate 3 still holds), so **not** fixing it.
   If it ever matters: a `draft_ids: set[str] | None` filter param, ~6 lines.
4. **The $0.20 is spent at Gate 3, outside the chassis.** Publishing 30
   accumulated drafts in one sitting is a real $6.00 the governor only saw as
   drafts. The execute-time over-reserve keeps them aligned when most drafts
   get published; it over-counts (fails safe) when most are declined.
5. **`sold_out`/`expired` states from the recent sync** distort the portfolio
   cap denominator (34 active, not 125 synced) — why §5 exempts rather than
   tunes a percentage.
6. **Fixture copy on a live listing** is the worst realistic failure mode.
   `precondition_ok = live_copy_open()` is the single control preventing it; it
   deserves its own assertion in review.
7. **Existing listings may already carry the §6.2 promise mismatch.** The
   6000 px bar is new; listings built under the landing default of 3000 px were
   published against `whatyougot` copy advertising 16x20 at 300 DPI. This is a
   pre-existing accuracy question (Etsy Seller Policy §1.c.4, the same clause
   E3 cites), not something this capability introduces — but it is now known.
   Audit the existing catalog's source resolutions and either trim the
   advertised sizes or correct the affected listings' copy. Tracked separately;
   **not** in this capability's scope.
