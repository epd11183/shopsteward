# Weekly Etsy Ops Run

The scheduled Claude Desktop task follows this file. Keep it current when tools
change; the task prompt only points here.

Run everything from the repo root of this checkout. Never read `data/` or `.env*`
(the CLI loads `.env` itself). Authority and limits: CLAUDE.md "Governance &
decision authority" (total control of listings, 2026-11-22; $20/month cap).

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
     under NEEDS YOU with their problems: fix the draft with `ops edit-listing`
     (or `listings` tools) and publish it at Gate 3;
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

## New listings (batches, operator-authorized)

New listings cost $0.20 each, inside the $20 cap. Create them in batches and
check each batch live before starting the next.

- **Batch 1:** 10 listings published 2026-11-22, IDs 4623573891 to 4623581618.
- **Before batch 2:** check batch 1 on Etsy. Confirm they are active and have
  5 files and 7 images each, and note their views.
- **Building a batch:** `shop build <winners folder> --live-vision --live-copy
  --live-etsy-write`.
  - Etsy drafts are not public. Publishing goes through `gate3.publish`, from
    the review UI (`uv run shopsteward serve`).
  - Read every draft back before publishing: subject-correct title, 5 files,
    13 tags, `listing_type` download.
- ⛔ `listings build` **without** `--live-etsy-write` writes FAKE Etsy IDs
  (1000+).
  - To recover: `listings reset <folder> --include-pushed --format
    digital_download --keep-landing`.
  - Then confirm exactly the fake IDs it lists with `--confirm-listing-id` and
    `--apply`.
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
- **Social:** the Buffer API adapter is designed
  (`docs/designs/2026-11-22-buffer-social-adapter.md`) but not built. Keep
  using the existing Buffer connector for posts until it ships.

## Report

End with a short report:
- what executed, with undo IDs;
- what is pending and why;
- spend this month against the $20 cap;
- anything that failed;
- one recommended next action.
