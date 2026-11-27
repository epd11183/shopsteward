# Weekly Etsy Ops Run

The scheduled Claude Desktop task follows this file. Keep it current when tools
change; the task prompt only points here.

Run everything from the repo root of this checkout. Never read `data/` or `.env*`
(the CLI loads `.env` itself). Authority and limits: CLAUDE.md "Governance &
decision authority" (total control of listings, 2026-11-22; autonomous new
listings behind the read-back gate, 2026-11-23; $20/month cap).

## Gates

**Run every command through `scripts/ops.sh`** (from the repo root), e.g.
`scripts/ops.sh sync --live`. It sets every live gate this runbook needs, then
runs `uv run shopsteward`. Do not set gates by hand and do not try to read
`.claude/settings.local.json` (the Desktop shell can't; the 2026-11-24 run set
gates by hand, missed three, and new listings and social posting silently
did nothing). If a command still says a gate is closed, stop and report it.

`ops run/approve/undo/edit-listing/replace-file` still need `--live-autonomy`
on the command itself to act on the real shop.

## The cycle

1. `scripts/ops.sh sync --live`. This pulls listings, receipts and reviews.
   Then `scripts/ops.sh ops buffer-sync --live` (read-only). It records Buffer posts and metrics, marks
   sent pins posted at their real send time, and feeds the per-channel pacing
   counts. Run it every week: pacing is wrong without it.
2. `scripts/ops.sh ops run`. This is a **dry run by default**: it records
   nothing and prints `would propose: ...` lines. Read them. Pending approvals
   reach NEEDS YOU only from the live step below.
3. `scripts/ops.sh ops run --no-dry-run --live-autonomy`. This executes
   every AUTO-tier action inside the governor's caps:
   - listing capabilities: `seo_edit`, `size_claims_fix`, `reprice`,
     `deactivate`, `renew`, `autorenew_off`;
   - `catalog_expand`: up to `max_new_per_week` (5) new listings from
     `D:/Photos/Etsy/candidates`, each published only if the read-back gate
     passes. Needs `SHOPSTEWARD_LIVE_COPY=1` and `SHOPSTEWARD_LIVE_VISION=1`
     too, or nothing is auto-published. Gate failures appear in the brief
     under NEEDS YOU with their problems. Fix them yourself (see "New
     listings" below), never hand them back;
   - social posts (NOTIFY since 2026-11-24): `social.pinterest_post` and
     `social.caption_draft` queue Pinterest, Instagram and Facebook posts in
     Buffer, at least 4 hours ahead, within each channel's weekly target
     (governor `PACE`). Each can be undone while still queued;
   - `listing.reprice` is AUTO again (2026-11-24) with two guards: no cuts
     while `reprice.blackout_until` (a sale end date; Etsy has no sales
     API) is today or later, and no cut within 30 days of an undo or of a
     price the operator raised by hand. If the operator starts a sale,
     set `blackout_until` to its end date (`ops config` change) before the
     run;
   - `autorenew_on` is AUTO (2026-11-27), only for listings that sold inside the
     dead-listing window;
   - `gapfill_reprint` is AUTO (2026-11-27): at most 1 new physical POD reprint
     a week, published only if the physical read-back gate passes. A gate
     failure stays a draft under NEEDS YOU; "check the Gelato dashboard for
     draft X" means a create attempt's outcome is unknown: never re-run it, report
     it;
4. `scripts/ops.sh ops brief`. Its sections are NEEDS YOU, DONE, REFUSED
   and THE SHOP. Report from it.
5. `scripts/ops.sh ops status`. Shows month spend against the $20 cap and
   whether ops is halted.
6. Missed-run alert: the Windows task "ShopSteward weekly-run heartbeat"
   (daily 09:00, `scripts/ops_heartbeat_alert.ps1`) also runs a read-only
   daily `sync --live` + `buffer-sync --live`, and pops up an alert when
   `ops heartbeat` finds no full live `ops run` (step 3) in 8 days, or when
   the daily sync fails. Only step 3 without `--capability` resets it.

Act-then-show: do listing fixes yourself and report them. Never hand a listing
fix back to the operator. Every executed action prints an
`ops undo <id> --live-autonomy` line. If something looks wrong, undo it and say
so.

## Tools added 2026-11-22

- `ops replace-file <listing_id> --file PATH --live-autonomy` swaps a digital
  listing's rank-1 full-frame download for a re-exported JPEG (for example, dust
  healed in Lightroom). It keeps the old file name and leaves ranks 2-5 alone.
  Since 2026-11-27 it runs immediately (AUTO, with undo) when the old file can be
  rebuilt from the archive and the new file matches the old photo. Otherwise it is
  HELD with the reason. A held swap has no undo: approve it with
  `ops approve <id> --live-autonomy` only after checking the new file is the same
  photo. At 5 files it deletes the old file first; if
  the upload then fails, re-run the command to resume.
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
- **Sensor dust** (gate problem `sensor dust: <where>`): the listing stays a
  draft. Never edit the photo. Report it with the location. Once the operator
  has healed and re-exported it: `ops replace-file <listing_id> --file PATH
  --live-autonomy` (swaps the draft's uploaded file; approve only after
  checking it is the same photo), then `listings publish --listing-id <id>
  --live` (re-runs the gate). If the gate was wrong (no dust visible), say so
  and leave the publish to the operator.
- **First live auto-published listings**: open each new listing's copy and
  compare it with its photo. The checker is good, not perfect (a wrong species
  is the hardest catch). Fix anything wrong with `ops edit-listing`; undo the
  action if the photo itself shouldn't be listed.
- **Candidate supply (before step 3, every run)**: run
  `scripts/ops.sh listings candidate-status --json`. It returns `listed`,
  `ingested`, `in_folder`, `failed` (base-name lists) and `needed` (a count).
  If `needed` > 0 and the Lightroom tools are available: search the catalog
  for rating >= 4; drop every base name (file name without extension, e.g.
  `1J4A2472.CR3` -> `1J4A2472`) in `listed`, `ingested`, `in_folder` or
  `failed`; prefer different scenes over near-identical frames of one burst or
  a series already listed; export at most `needed` as JPEG, quality 100, full
  size, sRGB, named `<base name>.jpg`, into `D:/Photos/Etsy/candidates`. Then
  run the visual duplicate check below on the new files (older listings have
  no source record, so `listed` misses them). Without Lightroom, or if fewer
  than `needed` qualify, report the shortfall. Report `failed` files (3 failed
  attempts): the operator moves them out or fixes them.
  The operator may also add exports by hand (full size, 6000px+ long edge,
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

## Social posting (cut over 2026-11-24)

ShopSteward posts to Pinterest, Instagram and Facebook itself, through the
Buffer API, in step 3. **Do not post, draft, schedule or delete anything
through the Buffer connector, MCP or CLI**, and do not write captions for
Buffer by hand: that was the old flow, and two posters on one queue means
double posts. Read-only Buffer lookups are fine.

- The brief's SOCIAL QUEUE shows each queued post with its due time and its
  undo-until time, plus what each channel still owes this week.
- To pull a post, `ops undo <action_id> --live-autonomy` while it is still
  queued. After it is sent it can only be removed on the platform itself:
  say so in the report.
- UNRESOLVED means a create outcome is unknown: run
  `ops buffer-sync --live`, which resolves it to queued or lost. Never post
  it again by hand. ERROR means a post failed in Buffer; it is never retried
  automatically. Report both.

## Hard limits

- **Spend:**
  - The cap is $20/month and is never raised.
  - No Etsy Ads spend.
  - `gapfill_reprint` costs only the $0.20 listing fee up front (Gelato free plan
    bills per order, out of the sale).
- **Photos:** AI never adds to a photograph. Generative removal of distractions
  is allowed. No AI label on social posts (operator 2026-11-22).
- **Data and git:** never commit shop data or credentials. No force-push. Don't
  merge PRs unless the operator says so.
- **Social:** posts go out only through `ops run` (Buffer API adapter,
  design `docs/designs/2026-11-22-buffer-social-adapter.md`), never the
  Buffer connector. See "Social posting" above.

## Report

End with a short report:
- what executed, with undo IDs;
- new listings published this run (IDs and titles) and any gate failures
  with what you did about them;
- candidates left in `D:/Photos/Etsy/candidates`;
- what is pending and why;
- spend this month against the $20 cap;
- social posts queued this run (channel, listing, due time) and any SOCIAL
  QUEUE UNRESOLVED/ERROR items;
- anything that failed;
- one recommended next action.
