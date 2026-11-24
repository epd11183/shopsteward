# Weekly Etsy Ops Run

The scheduled Claude Desktop task follows this file. Keep it current when tools
change; the task prompt only points here.

Run everything from the repo root of this checkout. Never read `data/` or `.env*`
(the CLI loads `.env` itself). Authority and limits: CLAUDE.md "Governance &
decision authority" (total control of listings, 2026-11-22; autonomous new
listings behind the read-back gate, 2026-11-23; $20/month cap).

## Gates

The env vars come from `.claude/settings.local.json`. If a command says a gate
is closed, prefix it inline, e.g. `SHOPSTEWARD_LIVE_AUTONOMY=1 uv run ...`.

| Command | Needs |
|---|---|
| `sync --live` | `SHOPSTEWARD_LIVE_ETSY_READ=1` |
| `ops run/approve/undo/edit-listing` live | `SHOPSTEWARD_LIVE_AUTONOMY=1` **and** `--live-autonomy` |
| `listings ...` Etsy writes | `SHOPSTEWARD_LIVE_ETSY_WRITE=1` |
| live copy / vision | `SHOPSTEWARD_LIVE_COPY=1` / `SHOPSTEWARD_LIVE_VISION=1` |

⚠ Running `ops approve` without the autonomy gate executes against a fake
adapter and burns the proposal. Check the gate first.

## The cycle

1. `uv run shopsteward sync --live`. This pulls listings, receipts and reviews.
   Optional until the S9 cutover: `SHOPSTEWARD_LIVE_BUFFER=1 uv run shopsteward
   ops buffer-sync --live` (read-only; needs `BUFFER_API_KEY`). It records Buffer
   posts and metrics and marks sent pins posted at their real send time. Without
   `--live` it uses an empty fake and does nothing.
2. `uv run shopsteward ops run`. This is a **dry run by default**. Read what it
   would do.
3. `uv run shopsteward ops run --no-dry-run --live-autonomy`. This executes
   every AUTO-tier action inside the governor's caps:
   - listing capabilities: `seo_edit`, `size_claims_fix`, `reprice`,
     `deactivate`, `renew`, `autorenew_off`;
   - `catalog_expand`: up to `max_new_per_week` (5) new listings from
     `D:/Photos/Etsy/candidates`, each published only if the read-back gate
     passes. Needs `SHOPSTEWARD_LIVE_COPY=1` and `SHOPSTEWARD_LIVE_VISION=1`
     too, or nothing is auto-published. Gate failures appear in the brief
     under NEEDS YOU with their problems. Fix them yourself (see "New
     listings" below), never hand them back;
   - capabilities still at PROPOSE stay pending: `gapfill_reprint`,
     `autorenew_on`, `social.*`.
4. `uv run shopsteward ops brief`. Its sections are NEEDS YOU, DONE, REFUSED
   and THE SHOP. Report from it.
5. `uv run shopsteward ops status`. Shows month spend against the $20 cap and
   whether ops is halted.
6. Missed-run alert: the Windows task "ShopSteward weekly-run heartbeat"
   (daily 09:00, `scripts/ops_heartbeat_alert.ps1`) pops up a desktop alert when
   `ops heartbeat` finds no live sync in 8 days. Step 1 is what resets it.

Act-then-show: do listing fixes yourself and report them. Never hand a listing
fix back to the operator. Every executed action prints an
`ops undo <id> --live-autonomy` line. If something looks wrong, undo it and say
so.

## Tools added 2026-11-22

- `ops edit-listing <listing_id> [--title T] [--tags "a,b,c"]
  [--description-file PATH] --reason "why" --live-autonomy`
  - Use it for any title, tag or description fix you write yourself.
  - It goes through the runner, so you get events, undo and the governor.
  - It allows one edit per listing per day.
  - Write descriptions as plain text in a scratch file. The tool unescapes
    Etsy's `&#39;` entities, so never paste HTML entities back.
- `ops run --capability listing.size_claims_fix` runs one capability only. It
  takes the same `--no-dry-run --live-autonomy` flags.
- `listing.size_claims_fix` removes sizes the delivered files don't cover. When
  it can't rewrite safely it returns nothing. In that case, hand-write the
  description and apply it with `ops edit-listing`.
- `listings backfill-files [--listing-id ID] [--live] [--rollback]` adds the
  4 multi-ratio crop files (ranks 2-5) to a live digital listing that has only
  the full-frame file.
  - Run it without `--live` first.
  - The digital files limit is 5 per listing and 20 MB each.
- Etsy rate limits (429) are retried automatically on reads. Writes are sent
  once.

## New listings (autonomous since 2026-11-23)

`catalog_expand` (step 3) creates up to 5 new listings a week from
`D:/Photos/Etsy/candidates`. Each draft is read back from Etsy (5 files,
7 images, 13 tags, "(Digital Download)" title) and its copy is checked against
the photo by a vision model. A pass publishes it ($0.20, counted in the cap).

- **Gate failure** (brief, NEEDS YOU): read the problems and look at the photo
  (the candidate file). Rewrite the wrong title/tags/description with
  `ops edit-listing <listing_id> ... --live-autonomy`, then publish with
  `listings publish --listing-id <id> --live`. It re-runs the gate and
  publishes only on a pass. If a second attempt still fails, leave it a draft
  and report it.
- **First live auto-published listings**: open each new listing's copy and
  compare it with its photo. The checker is good, not perfect (a wrong species
  is the hardest catch). Fix anything wrong with `ops edit-listing`; undo the
  action if the photo itself shouldn't be listed.
- **Candidate supply**: the operator adds new JPEG exports to
  `D:/Photos/Etsy/candidates` from time to time (full size, 6000px+ long edge,
  only photos not already listed). Already-listed or near-duplicate photos are
  moved to `D:/Photos/Etsy/candidates_excluded`, never deleted. Before step 3,
  if files were added since the last run, check the new ones against the live
  listings (a contact sheet of new candidates beside the live listings'
  first images) and move any duplicate to `candidates_excluded`. The built-in
  pHash dedupe misses the shop's older listings.
- **Report the supply**: the number of candidates left (files in the folder
  that are not yet listed). Under 10, say so first in the report so the
  operator exports more.
- ⛔ `listings build` **without** `--live-etsy-write` writes FAKE Etsy IDs
  (1000+).
  - To recover: `listings reset <folder> --include-pushed --format
    digital_download --keep-landing`.
  - Then confirm exactly the fake IDs it lists with `--confirm-listing-id` and
    `--apply`.
- ⛔ Don't run `shop build --regenerate`: it re-scores every photo for nothing.
- The Etsy token cannot delete listings (it lacks the `listings_d` scope). Fix
  drafts in place.

## Hard limits

- **Spend:**
  - The cap is $20/month and is never raised.
  - No Etsy Ads spend.
  - `gapfill_reprint` creates a paid POD SKU, so it stays operator-approved.
- **Photos:** AI never adds to a photograph. Generative removal of distractions
  is allowed. No AI label on social posts (operator 2026-11-22).
- **Data and git:** never commit shop data or credentials. No force-push. Don't
  merge PRs unless the operator says so.
- **Social:** the Buffer API adapter (`src/shopsteward/adapters/buffer/`,
  design `docs/designs/2026-11-22-buffer-social-adapter.md`) is built but not
  yet wired into a capability. Keep using the existing Buffer connector for
  posts until the cutover (design slice S9).

## Report

End with a short report:
- what executed, with undo IDs;
- new listings published this run (IDs and titles) and any gate failures
  with what you did about them;
- candidates left in `D:/Photos/Etsy/candidates`;
- what is pending and why;
- spend this month against the $20 cap;
- anything that failed;
- one recommended next action.
