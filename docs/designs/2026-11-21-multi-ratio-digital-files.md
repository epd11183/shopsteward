# Multi-ratio digital files for digital-download listings (2026-11-21)

Status: design, not implemented. Author: `architect` sub-agent; saved by the
orchestrator. Governance: architecture/interface changes are delegated
decisions (CLAUDE.md "Governance & decision authority"). No new dependency:
Pillow + ImageCms already in use (`pyproject.toml:14`,
`pipeline/landing.py:83-94`).

## 1. Problem (verified)

- Copy advertises `whatyougot.sizes` = 4x5, 5x7, 8x10, 11x14, 16x20, A2, A3,
  A4 ("JPEG 300 DPI, sRGB") from `config/defaults/mockups.json:32-35`. The
  list is global, applied to every photo, and feeds copy inputs
  (`pipeline/listings/copy.py:58-59,125,136`; the prompt asks the LLM to write
  "available sizes", `config/defaults/prompts/listing_copy.txt:2,28`) and the
  "what you get" mockup chips (`mockups/intents.py:376`).
- Push uploads exactly one file at the master's native ratio
  (`pipeline/listings/push.py:159-185`, `rank=1`,
  `name=Path(landing_path).name`).
- **The resolution claim is false for common sources.** 6000x4000: a 4:5 crop
  is 5000x4000, which can't do 16x20 at 300 PPI (needs 4800x6000). The A crop
  is 5657x4000, which can't do A2 (needs 4961x7016). Policy E16 condition 2
  (`docs/policy/2026-08-11-autonomy-platform-policy.md:57-63`) says a "6000
  px long edge" delivers 16x20. That holds only for a 4:5 source.
- Existing single-file path flaws:
  - The derived re-encode drops the ICC profile (`pipeline/listings/images.py:63-66`).
  - `landing_original` uploads raw bytes, including camera EXIF/GPS (`images.py:59-61`).
  - Landing dimensions are the raw `img.size` (`pipeline/landing.py:113`), not
    EXIF-transposed, so orientation can be wrong in copy (`copy.py:102-107`).

## 2. Ratio set and size mapping

Etsy limits:
- 20 MB per file, encoded at `docs/designs/2026-07-14-m5a-digital-listings.md:248`
  and `config/defaults/listing.json:54`.
- **5 digital files per listing, confirmed by the operator 2026-11-21.** The API
  reference for `uploadListingFile` doesn't state it. Keep it in config
  (`max_files`) and have the fake enforce it. This is separate from the
  listing gallery cap of 20 photos and 2 videos.

| Rank | Key | Ratio | Delivers | Tolerance |
|---|---|---|---|---|
| 1 | `full` | native | 4x6, 8x12, 12x18, 16x24, 20x30 only if native is within 1% of 2:3; else "full frame, no standard size" | ≤1% |
| 2 | `4x5` | 4:5 | 4x5, 8x10, 16x20 | exact (±1 px) |
| 3 | `5x7` | 5:7 | 5x7 | exact |
| 4 | `11x14` | 11:14 | 11x14 | exact |
| 5 | `A` | 210:297 | A4, A3, A2 | exact |

- 5x7 gets its own file rather than borrowing the A crop. 3:4 was rejected
  because 18x24 needs about 44 MP from a 3:2 source. Swapping them later is a
  config edit.
- Orientation follows the photo, using dimensions after `ImageOps.exif_transpose`.
  Square sources are treated as portrait.
- A crop that keeps less than `min_retained_fraction` (0.6) of the source area
  isn't shipped. This catches panoramas.

| Source | 4:5 | 5:7 | 11:14 | A | full 2:3 |
|---|---|---|---|---|---|
| 24 MP 6000x4000 | 4x5, 8x10 (no 16x20) | 5x7 | 11x14 | A4, A3 (no A2) | 4x6, 8x12, 12x18 |
| 45 MP 8256x5504 | 4x5, 8x10, 16x20 | 5x7 | 11x14 | A4, A3, A2 | +16x24 |

## 3. Resolution rule

- A size is advertised only if the crop is at least `round(inches × ppi)` px on
  both axes, with `ppi` = 300 from config. A-sizes are converted from mm.
- Failing sizes are dropped per photo. They are never downgraded: no "150 DPI"
  wording, no upscaling, nothing generative.
- A ratio file that delivers zero sizes isn't shipped, except `full`, which
  always ships.
- E16 `min_long_edge_px` stays as a catalog-quality floor only.

## 4. File plan: one source of truth

New pure module `src/shopsteward/pipeline/listings/digital_files.py`. It does no
DB or event I/O, like `images.py`. It lives in `pipeline/listings` because the
import-linter forbids pipeline → mockups (`pyproject.toml:71-77`).
- `photo_dims(path) -> (w, h)`, after EXIF transpose.
- `plan_files(dims, focus, cfg) -> FilePlan`, returning `[{key, rank, crop_box, sizes}]`.
  Ranks are compacted.
- `encode_file(path, entry, cfg) -> (bytes, sha256)`. Steps, deterministic:
  1. `exif_transpose`, then crop.
  2. Convert to sRGB through ImageCms if the source ICC isn't sRGB, and embed
     the sRGB ICC.
  3. Set `dpi=(ppi, ppi)`.
  4. Write fresh minimal EXIF: Artist and Copyright only. No GPS, no serials,
     no Orientation.
  5. Use the quality ladder, factored out of `images.py:24,63-74` into
     `encode_jpeg_under_cap(img, max_bytes, **save_kw)`.
  6. The copyright year comes from EXIF DateTimeOriginal, so the bytes stay
     deterministic. XMP is skipped in v1.
- Plan recorded once at draft build: `listingdraft.files_planned {draft_id,
  source_dims, focus, files, config_hash}`, appended after
  `listingdraft.images_selected` (`pipeline/listings/drafts.py:224-237`) and
  projected into `proj_listing_drafts.files_plan_json`.
- Consumers of the plan:
  - Copy reads the plan.
  - The "what you get" panel renders the plan's sizes and refuses to build on a
    mismatch.
  - Push re-plans and fails with "file plan drift" if the result differs from
    the recorded plan.

## 5. Focal point (operator override, no saliency)

- Events: `cropfocus.set {landing_file_id, x, y}` with x, y in [0, 1], and
  `cropfocus.cleared {landing_file_id}`. user_id is set on both.
- Projection: `proj_crop_focus(user_id, landing_file_id, x, y, updated_at)`, PK
  (user_id, landing_file_id), last write wins, rebuilt in `rebuild_listings`.
- Default focus is (0.5, 0.5). The crop window is centred on it and clamped to
  the frame.
- The key is the landing sha256 (`pipeline/landing.py:23-28,52-61`). A
  re-exported master gets a new key, so its focus resets. This is documented.
- CLI:
  - `shopsteward listings crop-focus <landing_file_id | --listing-id N> --x --y | --clear`
  - `shopsteward listings crop-preview <id> --out <dir>`
- Setting focus on an already-pushed draft warns only. v1 does not replace files.

## 6. Push-stage changes

- `_file_stage` (`push.py:159-185`) loops over the plan in rank order. For each
  file it calls `encode_file`, then `upload_listing_file(..., rank=entry.rank)`,
  then appends one `listingdraft.file_attached` with new fields `key, rank,
  name, sizes`. Old events with no key read as `full`.
- Filenames follow `<slug>_<key>_<sizes>.jpg`, e.g. `misty-ridge_4x5_4x5-8x10.jpg`.
- Retry: gate3 resumes at the `file` stage (`gate3.py:333-344`). A `skip_keys`
  set built from prior `file_attached` events mirrors `already_attached_ranks`
  (`gate3.py:306-313`).
- New projection `proj_listing_files(user_id, etsy_listing_id, draft_id, key,
  rank, etsy_file_id, sha256, sizes_json)`.
- Adapter changes:
  - New read `list_listing_files(listing_id)` on `EtsyWriteAdapter`
    (`adapters/etsy/interface.py:120-150`).
  - The fake rejects a 6th file and anything over 20,000,000 bytes
    (`fake.py:191-204`).
  - The live adapter mirrors `live.py:292-301`.
  - `delete_listing_file` arrives in slice 4.
- Feature flag: `ListingConfig.digital_files: DigitalFilesConfig | None = None`
  (`pipeline/listings/models.py:85-96`). `None` keeps today's behaviour.
- Config delivery: `seed()` is a no-op once a config is seeded
  (`pipeline/listings/config.py:30-41`). Add `listings config sync-defaults` and
  `listings config set <dotted.key> <json>`; they would be the first producers
  of `listingconfig.updated`.
- `digital_files.copyright_holder` is seeded as "PhotosByEricD" (the shop name,
  operator decision 2026-11-21) from `config/defaults/listing.json`, so it lives
  in the DB, not in code, and can be changed with `config set`. If it's null,
  Copyright is omitted and a warning is printed. EXIF ASCII can't hold "©", so
  the tag reads "(c) <year> PhotosByEricD".

## 7. Copy and "what you get" changes

- Remove the global `whatyougot.sizes`. Sizes become per-photo
  (`mockups/models.py:103`), applied through `model_copy(update=...)` in
  `mockups/jobs.py`.
- Copy changes:
  - `copy.py:136` takes its sizes from the plan.
  - A deterministic "Files included" block is appended (the same mechanism as
    `copy.py:217-219`).
  - The prompt drops "available sizes".
  - Guard: copy that mentions a size the plan doesn't deliver is refused
    through the retryable refusal path (`copy.py:160-165`).

## 8. Backfill for live listings (shop 52644245)

`shopsteward listings backfill-files [--listing-id N] [--live]`, dry-run by
default.
1. **B0 (read-only):**
   - Resolve the source with `resolve_source`; if there's no draft, "adopt first".
   - Call `list_listing_files`, then `plan_files` for the missing ranks 2..5.
   - Report every listing whose copy or panel claims a size its plan can't deliver.
2. **B1 (LIVE WRITE):** upload at most 4 crops per listing. Requires `--live`
   and `live_etsy_write_open()`. The existing rank-1 file is kept. NOTIFY
   eligible: free, reversible, no effect on search ranking.
3. **B2 (LIVE WRITE, PROPOSE):** a description edit through `listing.seo_edit`'s
   PROPOSE path (`ops/capabilities/seo_edit.py:8-16`). Always B1 before B2.
4. **B3 (LIVE WRITE, PROPOSE):** replace the "what you get" image. This needs
   an image replace method, which is a separate slice.

Verify before B1: whether past buyers see newly added files.

## 9. Test plan (fixture-first, no live calls)

- Smallest proving test: a fake push of a synthetic 600x400 JPEG with `ppi`=30.
  It checks:
  - 5 uploads with ranks 1..5 and the expected names, plus 5 `file_attached` events;
  - the `4x5` file is 500x400 with dpi (30, 30), an sRGB ICC, Artist and
    Copyright, and no GPS;
  - a second encode produces the same bytes.
- `plan_files`: landscape, portrait, square and panorama sources; focus
  clamping; the tables above; rotated EXIF.
- Retry: the 3rd upload fails, and the retry uploads only files 3..5.
- The fake rejects a 6th file and a file over 20 MB.
- Copy guard; focus events and projection; backfill dry-run; the plan-drift
  failure.

## 10. Slices

1. Pure module, encoder and tests.
2. Config, events, projections and CLI (focus, preview, config set/sync).
3. Build plan, copy, panel, push, gate3 retry, adapter read and fake limits. In
   the same PR, amend policy E16 conditions 1 and 2 and reconsider
   `ops.json:76`.
4. Backfill B0/B1 plus `delete_listing_file`.
5. B2/B3 (PROPOSE).

## 11. Rejected

- Automatic saliency cropping: it can chop the subject, and the operator
  doesn't want it.
- A single ZIP: buyers can't see what they get, and mobile buyers struggle
  with ZIPs.
- An "up to 300 DPI" claim: still inaccurate.
- Each consumer computing sizes separately: risks drift between copy, panel and push.

## 12. Risks and rollback

Risks: a crop could exceed 20 MB (the quality
ladder and `SellableFileTooLargeError` handle it); focus resets on re-export;
backfilled listings keep their legacy rank-1 file.

Rollback: `config set digital_files null`; backfilled files are removed with
`delete_listing_file`.

Trip criteria: any upload rejection on ranks 2..5, a buyer crop complaint,
production plan drift, or an encode below q85.

## 13. C-suite critique (PRD §8.3)

- **CTO:** One plan event is right; the hazard is byte determinism across
  Pillow upgrades. Pin the Pillow minor version and add a golden-sha test.
- **CFO:** There's no API cost, and none of it comes out of the $20 cap. The
  real cost is evenings plus possible lost 16x20/A2 sales on 24 MP listings.
  The B0 audit should report how many live listings lose a claimed size.
- **CMO:** "Prints exactly at 8x10, 11x14, A4" sells once it's true. Add a
  stable "5 print-ready files, no cropping needed" phrase to the Files-included
  block.
- **CPO:** Ratio-named files let a buyer print without an editor. Make the
  description's first line a which-file-for-which-frame guide, and show crop
  previews on the Gate 3 card.
- **Chief Legal:** This fixes a live §1.c.4 accuracy exposure and a GPS/serial
  privacy leak. Require a non-null `copyright_holder` for any `--live` push or
  backfill, and amend E16 in the slice-3 PR.
