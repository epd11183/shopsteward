# Design: The Source-Asset Head (prerequisite for M8b gap-fill reprints)

**Status: PROPOSED, awaiting operator approval. No code until approved.**
**Date:** 2026-08-11. **Author:** architect subagent + orchestrator synthesis.
**Prerequisite for:** M8b gap-fill (slice 5). **Un-defers** part of the parked
POD listing-creation surface — see PRD/CLAUDE.md deltas.

## Scope

Build the minimum infrastructure so a future M8b capability can, given a
best-selling Etsy `listing_id`, resolve its source photograph, retrieve the
original print master, and build a NEW POD product_type draft that lands at
Gate 3 — WITHOUT the operator re-dropping the source photo in the landing
folder. Deliver the head only (linkage + retrieval + durable store); the
gap-fill capability itself is future M8b, described here only to prove the head
is sufficient.

## Prerequisite answers (from the code)

**1. Does a durable linkage already exist (listing_id → photo_id → source)?**
Partially, and it is event-sourced. `proj_listing_drafts` already carries
`landing_file_id`, `photo_id`, and `etsy_listing_id` on one row
(`projections.py:71-84`); POD writes `etsy_listing_id` at
`listingdraft.provider_linked`, digital at `listingdraft.pushed_to_etsy`; both
originate from `listingdraft.created` (carries `photo_id`+`landing_file_id`,
`pod/build.py:301-314`). So `etsy_listing_id → draft → photo_id →
landing_file_id` is reconstructable from the immutable log. **Gap:** best-sellers
come from `proj_sale_items.listing_id` (INTEGER) and only join back to a draft
if THIS pipeline created the listing; a manual/pre-pipeline listing has no draft
row → no photo_id. Type seam: `proj_sale_items.listing_id` INTEGER vs
`proj_listing_drafts.etsy_listing_id` TEXT — the join must CAST. Linkage is a
lookup, not new storage — but it is lossy and must fail honestly.

**2. Are the original source files durably available by photo_id?** **No.** Every
print-master read resolves `proj_landing_files.path` (on-disk) at build+link time
(`printfile.py:41-60`, `provider.py:120-125`). The landing folder is transient
(CLAUDE.md). The projection retains the recorded PATH forever, but the BYTES are
not preserved: `print_file_key`/`sha256` point at a TTL-expiring hosted
*derivative* (a re-encoded sellable, not the master; the signed URL is never
stored). **A stale path but no durable master** — so for a sale weeks old,
`os.path.exists(path)` is likely False. This is the decisive fact: a managed
store is required, not optional.

**3. Which reprint target is realistic?** New POD product_type of an existing
best-seller. `build_pod_drafts` needs no mockup set (`pod/build.py:28-29`); a new
product_type yields a fresh non-colliding `draft_id`
(`sha256(photo_key|pod_config_hash|provider|product_type)`). A digital restock
needs cv2 + a mockup set — out of scope for the head.

## Decision: reuse-on-disk (a) vs managed store (b) — BOTH, layered

Answer 2 settles it: gap-fill is *precisely* the case where the landing folder
was cleared, so (a) alone fails exactly when needed. Keep (a) as a fast path and
as the *source of bytes to archive*; add (b) as the durable copy.

- **Archive location: local disk `data/asset_store/`.** Boring, free, no new
  external service; consistent with where masters + the SQLite DB already live,
  and inherits the hard guardrail (`data/` is never read/printed/committed).
  **Reject R2 for the archive:** R2 is the transient public *host* for Gelato to
  fetch; the archive is durable, private, read only by us — R2 adds egress/cost
  and a public credential surface for no benefit. R2 stays the host; local disk
  is the archive.
- **What is archived: the ORIGINAL master bytes, verbatim** (`shutil.copyfile` of
  the resolved source — TIFF master when `prefer=tiff_master`, else sRGB JPEG).
  No resize, no re-encode, nothing generative (CLAUDE.md: AI never touches the
  photograph). The deterministic sellable re-encode still happens downstream at
  build time from the archived original.

## Identity / linkage model

No new linkage table — reuse `proj_listing_drafts`. Add one pure read helper:
`resolve_source(conn, user_id, etsy_listing_id) -> SourceRef | None` returning
`{photo_id, landing_file_id, on_disk_path, on_disk_present, archived}`. Selects
the draft by `CAST(etsy_listing_id AS INTEGER)=?`, checks `proj_landing_files` +
`os.path.exists` + `proj_asset_store`. No matching draft (manual/pre-pipeline
listing) → `None`, and the caller surfaces "not reprintable — no linked source"
(honest-gap spirit of `data_quality_notes`).

## Events / projections (the managed store)

Event-sourced, immutable, `user_id` on every row:
- Event **`asset.archived`** `{photo_id, sha256, bytes, width, height, format,
  stored_key, source_landing_file_id}`. `stored_key` is a RELATIVE key
  `f"{photo_id}/{sha256}.{ext}"` — never an absolute path or credential (mirrors
  `print_file_hosted` storing `file_key`, not the URL). Emitted where the print
  master is first resolved in `build_pod_drafts`, next to
  `listingdraft.print_file_prepared` (`pod/build.py:400-416`).
- Projection **`proj_asset_store(user_id, photo_id, format, sha256, stored_key,
  width, height, bytes, source_landing_file_id, archived_at, PK(user_id,
  photo_id, format))`** — keyed by format so a TIFF+JPEG pair coexists and
  gap-fill honors `pod.print_file.prefer`. Idempotent (same original ⇒ same
  sha256; re-archive is a no-op).
- Extend `resolve_print_source_path` (`printfile.py:32-60`) with a fallback: if
  the landing row is missing OR its path doesn't exist, resolve from
  `proj_asset_store` by `(photo_id, preferred format)` and return the archived
  file's absolute path (archive root from config + `stored_key`). This one change
  makes the EXISTING `prepare_print_file` → build chain serve gap-fill unchanged.
- Config `config/defaults/asset_store.json`: `{"root": "data/asset_store",
  "enabled": true}` (config-over-code, DB-seeded). No new dependency (`shutil`,
  `hashlib`, `os` are stdlib).
- **CTO improvement (accepted):** on READ, verify the archived file's sha256
  against the `asset.archived` payload, so disk bit-rot / a truncated copy fails
  loudly instead of shipping a corrupt master to a customer.

## How gap-fill (future M8b) consumes the head → Gate 3

1. Best-seller `listing_id` from `analytics.top_sellers` (**CMO improvement: rank
   the reprint queue by top-seller revenue**).
2. `resolve_source` → `photo_id` (+ confirm `archived`). No linkage / no archive
   → report "not reprintable," stop. (**CPO improvement: the M8b surface shows the
   reprintable/not-reprintable split up front with the reason.**)
3. A thin `build_pod_reprint(conn, user_id, photo_id, product_type)` reuses
   `_select_and_price` + the print-file event emission from `pod/build.py`,
   sourcing dims from `proj_asset_store` and the master via the extended
   `resolve_print_source_path`. New product_type ⇒ new `draft_id`, no collision.
4. `link_pod_drafts` → `enrich_pod_drafts` → `push_drafts`, fake/offline by
   default via existing live-gates — no change.
5. Draft lands in `gate3.queue()`; Gate 3 is the sole publish authority.
   **No new autonomy tier** (a draft needs none — draft #16).

## Slice plan (smallest first)

- **Slice 1 — linkage resolver + freshness check.** `resolve_source` over
  existing projections + `os.path.exists`. No storage, no events. Test: build a
  POD draft to `provider_linked`, delete the on-disk landing file, assert
  `resolve_source(listing_id)` returns the right `photo_id`,
  `on_disk_present=False`.
- **Slice 2 — managed store.** `asset.archived` event, `proj_asset_store`,
  copy-hook in `build_pod_drafts`, archive fallback + sha256-verify in
  `resolve_print_source_path`, `asset_store.json`. Test: build a POD draft,
  delete the landing file AND the derived host object, assert `prepare_print_file`
  still returns bytes whose sha256 matches the archived original (reprint works
  with the landing folder gone).
- **Slice 3 (future M8b, NOT this head) — `build_pod_reprint` + capability.** Out
  of scope now; listed to prove the head is sufficient.

**Rollback:** Slice 2 is additive (new event, new projection, gated by
`asset_store.enabled`). Disable the flag ⇒ no archiving, resolver falls back to
on-disk only, build/push/Gate 3 unchanged. No event mutated or deleted.

## PRD / CLAUDE.md deltas (operator review required)

- **Un-defers part of shop-building.** CLAUDE.md "Current focus" parks the
  listing-creation surface; this head touches parked POD code + adds a projection
  → architecture change + first PR of a new sub-milestone ⇒ operator sign-off.
- PRD §10 gains an "M8b-prerequisite: source-asset head" entry.
- New event `asset.archived` + table `proj_asset_store` (with `user_id`) added to
  the schema snapshot.
- No new dependency, no new external service, no AI/provider decision.

## Load-bearing assumptions

1. A best-seller worth reprinting was created by THIS pipeline (has a draft row);
   manual/pre-pipeline listings are not reprintable — stated ceiling, surfaced
   honestly, acceptable for MVP.
2. `photo_id` is stable/unique per photograph across ingests (the archive key).
3. Archiving at build time is early enough (original still on disk when the draft
   is first built).
4. Local disk under `data/` is durable enough for a single-operator tool (backed
   up with the operator's existing `data/` backup, like the DB).

## C-Suite critique (improvements folded into the design above)

- **CTO.** Right seam — one `resolve_print_source_path` change makes the whole
  build chain reprint-capable, no fork. *Improvement (accepted):* sha256-verify
  the archived master on read so bit-rot fails loudly.
- **CFO.** Local disk over R2 is correct — zero marginal cost; reprints monetize
  an already-paid-for photo at POD margins with no new shoot. *Improvement:*
  record archive bytes/count in the ops report so storage growth is visible.
- **CMO.** Reprinting proven best-sellers is the highest-confidence catalog
  expansion — demand is demonstrated. *Improvement (accepted):* prioritize the
  gap-fill queue by top-seller revenue.
- **CPO.** The "not reprintable" path is the UX risk — an operator expects any
  best-seller to be reprintable. *Improvement (accepted):* show the
  reprintable/not split up front with the reason.
- **Chief Legal.** Archiving only the operator's own masters, verbatim, no AI
  transform, under the never-committed `data/` tree keeps us clean on "AI never
  touches the photograph" and on image rights / public-repo exposure.
  *Improvement:* confirm POD provider terms permit re-submitting the same master
  to a new product_type without re-licensing.

## Operator decisions this design needs

1. **Approve un-deferring** the POD listing-creation surface for this head
   (architecture + first-PR gate).
2. **Confirm archive location** `data/asset_store/` (local) — reject R2 for the
   archive — or direct otherwise.
3. **Confirm the ceiling:** only pipeline-created listings are reprintable in the
   M8b MVP.
4. **Approve** adding `asset.archived` + `proj_asset_store` + `asset_store.json`
   to the schema snapshot.
5. **Approve the slice order** (linkage resolver → managed store), gap-fill
   (Slice 3) deferred to M8b proper.

---

*Nothing above is approved. No code until §"Operator decisions" 1/2/5 land.*
</content>
