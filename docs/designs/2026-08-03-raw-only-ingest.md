# M2a Design — RAW-Only Ingest

*Status: DRAFT, pending operator review (PRD §8.2). Architect output 2026-08-03.
Read-only design; not implemented. Amends `docs/designs/2026-07-03-m2-editing-module.md`
(ingest) and PRD §5.1 / decision 17. New PRD §13 decision candidates are numbered
**57–63** (50–56 are taken by the corrections proposer). Resolves the open decision
raised at `docs/designs/2026-08-03-corrections-proposer.md` §15b.1.*

---

## 1. The finding, and exactly what it costs

`src/shopsteward/editing/ingest.py:129-142` — a RAW whose `stem_key` has no JPEG
emits `photo.unpaired{reason:"missing_jpeg"}` and `continue`s. No `photo.ingested`
is appended, so no `proj_photos` row exists, so the photo does not exist to the
system at all: not to scoring, not to Gate 1, not to dispatch, not to the landing
matcher, not to M2b corrections. `ingest.completed.unpaired` is the only trace.

**Every consumer of the paired JPEG, enumerated.** This is the load-bearing list —
it decides §3.

| # | Consumer | How it reads | Breaks without a JPEG? |
|---|---|---|---|
| 1 | `editing/ingest.py:165` `_extract_exif(jpeg_path)` | `PIL.Image.open(path)` | producer side |
| 2 | `editing/projections.py:17,66` `proj_photos.jpeg_path` | `TEXT NOT NULL` | schema |
| 3 | `pipeline/scoring.py:158` `_candidates` → `ScoreContext.jpeg_path` | SELECT + string | yes |
| 4 | `pipeline/scorers/technical.py:22` | `cv2.imread(ctx.jpeg_path)` — **path** | yes, raises → `photo.score_failed` |
| 5 | `pipeline/scorers/commercial.py:15` → `pipeline/imaging.py:11` | `Image.open(path)` — **path** | yes |
| 6 | `pipeline/api.py:82-87` Gate 1 preview | `FileResponse(row["jpeg_path"])` — **path** | yes, the card has no image |
| 7 | *(unmerged, M2b)* `editing/corrections.py` `measure()` | `cv2.imread` — **path** | yes |

Touch `proj_photos` but **not** the pixels, and so are unaffected either way:
`pipeline/gate1.py:29,43` (`base_name`), `pipeline/landing.py:44` (`base_name`,
`photo_id`), `editing/dispatch.py:51` (`raw_path` → Lightroom), `editing/cli.py:86`
and `editing/api.py:130` (status counts). `mockups/` and `pipeline/listings/` read
`proj_landing_files`, never `proj_photos`.

**All four live pixel consumers take a filesystem path, not bytes.** None of them
has a bytes entry point. That single fact settles §3.

---

## 2. Proposal (one paragraph)

`ingest_folder()` stops skipping RAWs that have no paired JPEG. Instead it extracts
the RAW's embedded camera preview via a new one-function module
`src/shopsteward/editing/rawpreview.py` (lazy `import rawpy`, measured at **9 ms**
for a Pillow-openable **8192×5464** JPEG on the operator's CR3), writes it once to
`data/derived/<photo_id>.jpg`, and emits the ordinary `photo.ingested` with
`jpeg_path` pointing at it plus a new payload field `jpeg_source`. No new event
type, no consumer change, no adapter, no network. Every ingested photo continues to
have exactly one readable JPEG on disk — the invariant that keeps all seven
consumers above untouched. A RAW that cannot yield a usable preview still emits
`photo.unpaired`, with a `reason` that says why.

---

## 3. Derive at ingest, not lazily — and why it is not close

```
folder/IMG_1234.CR3 (no JPEG)
   │
   ├─ sha256(RAW) ────────────────────────────► photo_id (unchanged, content-addressed)
   │
   └─ rawpreview.extract_preview_jpeg()        ~9 ms
         ├─ ThumbFormat.JPEG  → bytes verbatim ─┐
         ├─ ThumbFormat.BITMAP → Pillow encode ─┤
         └─ no/undersized/unsupported preview   │
              └─ hero: postprocess() 1.14 s ────┤   mass: refuse → photo.unpaired
                                                v
                              data/derived/<photo_id>.jpg   (tmp + os.replace)
                                                v
   photo.ingested{ …, jpeg_path: <derived>, jpeg_source: "raw_preview", exif }
                                                v
        proj_photos.jpeg_path  ──►  technical · commercial · Gate 1 preview · corrections
                                    (all four unchanged, all four path-based)
```

**Lazy derivation was rejected on the consumer table, not on taste.** Deriving on
first use requires each of consumers 4, 5, 6 (and 7) to (a) detect a photo with no
pixels, (b) call a decoder, and (c) materialise a path anyway — `cv2.imread` and
`FileResponse` cannot be handed bytes. So lazy derivation writes the same file, just
later, from an HTTP request thread inside `GET /gate1/photo/{id}/preview`, four
times over in four modules, with four chances to forget. It also spreads a `rawpy`
import into `pipeline/`, which today has no reason to know RAW exists. The
"avoided" write is not avoided; it is deferred and duplicated.

The eager cost is bounded and known: **9 ms/frame**, ~18 s for a 2,000-frame event,
against 38 min if the same design demanded a demosaic. Disk is the real cost and it
is currently **unmeasured** — the slice-1 probe prints preview bytes on real files
before slice 2 merges (§14). Assume single-digit MB/frame; a 2,000-frame RAW-only
event adds roughly 8–16 GB beside the ~50 GB of CR3s it sits next to. That is a
regenerable cache on a machine that already holds the masters.

**No half-state.** `jpeg_path` is never null, never `""`, and `jpeg_source` is never
`"none"`. A "photo that exists but has no pixels" would push the missing-JPEG branch
into every consumer — the exact cost lazy derivation was supposed to avoid, smuggled
back in as a schema state. If the operator does not want derived files, the knob is
`ingest.derive_from_raw: false`, which restores today's skip behaviour exactly (§11).

---

## 4. Where derived files live

`data/derived/<photo_id>.jpg`, where `photo_id` is the RAW's sha256 — the identity
the system already uses. New `settings.py` entry, matching the four that exist:

```python
def derived_dir() -> Path:
    return Path(os.environ.get("SHOPSTEWARD_DERIVED_DIR", "data/derived"))
```

- **Owner:** the editing module. `ingest.py` is the only writer, ever.
- **Configurable:** yes, by env var, like `bridge_dir()` / `landing_dir()` /
  `mockups_dir()`. Not per-mode, not per-run.
- **Committed:** impossible. `data/` is gitignored (`.gitignore:8`) and
  agent-read-denied; `*.cr3` is separately ignored.
- **Content-addressed, not name-addressed.** Two folders can both hold `IMG_1234`;
  two operators can hold the same file. A sha256 filename cannot collide, and
  re-deriving the same RAW always produces the same bytes at the same path.
- **Not user-partitioned.** Single-operator v1; content addressing means a shared
  file can never serve wrong pixels. A `<user_id>/` level is the multi-tenant
  follow-up, noted, not built.

**Precedent check before inventing:** `pipeline/listings/images.py` already has a
"derived JPEG" concept and deliberately **never persists it** (module docstring:
"callers never need to persist a derived JPEG"). That precedent does not transfer —
it derives from a landing original on the push path, where the consumer takes bytes.
Ours has four path-based consumers. `mockups/jobs.py:156` is the closer precedent: a
generated-image tree under `data/`, addressed by `photo_ref`, env-overridable. This
follows that.

**If the operator deletes it** (Explorer, disk cleanup, whatever): it is a cache and
deleting it is supported. Consequences and recovery:

| Surface | Behaviour with the file gone | Recovery |
|---|---|---|
| technical scorer | raises → `photo.score_failed` (existing path) | re-run ingest, re-run scoring |
| Gate 1 preview | 404/`FileResponse` error on that card | re-run ingest |
| corrections (M2b) | abstains, reason `unreadable` (already specced) | re-run ingest |
| dispatch → Lightroom | unaffected — uses `raw_path` | — |

Recovery is **`shopsteward ingest <same folder>`**. The dedupe branch (§5) is
extended: on `photo.duplicate_skipped`, if the recorded `jpeg_source != "camera"`
and the derived file is missing while the RAW is present, re-derive it silently.
Same input bytes → same output bytes → same path, so the original `photo.ingested`
remains true and **no event is appended**. Cost: one `Path.exists()` per duplicate.
There is no `prune` command; deleting the folder is the prune command.

Known limitation, unchanged by this design and deliberately not fixed: if the
operator deletes a *camera* JPEG for an already-ingested pair, that photo's
`jpeg_path` dangles forever — dedupe makes it un-reingestable, and inventing a
derived preview would make the recorded `jpeg_source: "camera"` a lie. The clean fix
is a `photo.jpeg_resourced{photo_id, jpeg_path, jpeg_source}` event that the
projection folds last-write-wins. Specced here in one line so a future PR does not
have to think; not built (no operator has hit it).

---

## 5. Event model — the subtle part, resolved by reading the fold

**No new event type.** `photo.ingested` gains one field:

```
photo.ingested { photo_id, ingest_job_id, base_name, raw_path, jpeg_path,
                 raw_sha256, exif, mode, status,
                 jpeg_source: "camera" | "raw_preview" | "raw_postprocess" }   ← NEW
```

`photo.unpaired` keeps its type and gains two `reason` values:

| `reason` | Meaning |
|---|---|
| `missing_raw` | *(existing)* JPEG with no RAW — untouched, §8 |
| `missing_jpeg` | *(existing)* **emitted only when `derive_from_raw: false`** |
| `raw_preview_unavailable` | usable preview absent/undersized and `postprocess` fallback is off for this mode (mass default) |
| `raw_undecodable` | rawpy could not open the file, or `postprocess()` itself failed; payload carries `error:{code,message}` |

`ingest.completed` gains `derived` (count), mirrored into `IngestReport.derived` and
a `proj_ingest_jobs.derived INTEGER NOT NULL DEFAULT 0` column. Read with
`p.get("derived", 0)` so historical events fold. This is the only new counter and it
is what makes "that folder was RAW-only" visible in `edit status`.

**Does a prior `photo.unpaired` block a later `photo.ingested`? No — verified.**
`_known_raw_hashes()` (`ingest.py:52-59`) reads **only** `photo.ingested` events and
keys on `raw_sha256`. A RAW that was skipped never recorded a hash, so on the next
run it is not a duplicate, takes the normal path, and emits `photo.ingested`
normally. **Re-running `shopsteward ingest <folder>` is the entire backfill
mechanism.** No migration, no `--repair` flag, no replay tool, no event surgery.

**Does the projection need to reconcile a photo with both events?**
No — and this is worth stating precisely because it looks like it should.
`rebuild_editing()` (`projections.py:53-136`) has **no branch for `photo.unpaired`
at all**. Unpaired events carry a `path` and no `photo_id`, so they never produced a
`proj_photos` row to reconcile against. After a re-run the log reads:

```
t0  photo.unpaired  {ingest_job_id: J1, path: …/IMG_1234.CR3, reason: "missing_jpeg"}
t1  photo.ingested  {ingest_job_id: J2, photo_id: <sha>, jpeg_source: "raw_preview", …}
```

which is a *correct* history — at t0, with that code, the file was skipped; at t1 it
was ingested — and it folds to exactly one `proj_photos` row. `proj_ingest_jobs`
keeps J1 at `unpaired=1` (accurate, historical) and J2 at `paired=1, derived=1`.
Nothing double-counts, nothing is updated, nothing is deleted. Append-only holds
without effort, which is the sign the event model is right.

Pre-existing behaviour, unchanged and worth naming because the operator will see it:
`photo.unpaired` has no dedupe, so a JPEG-orphan re-emits an unpaired row on every
run. True today; not this PR's problem.

**Projection changes:** `proj_photos` gains `jpeg_source TEXT NOT NULL`, folded as
`p.get("jpeg_source", "camera")` — correct by construction, since every historical
ingest was a camera JPEG. Drop-and-rebuild means no migration.

**`user_id`:** unchanged — every event carries it; `proj_photos` and
`proj_ingest_jobs` keep it in the PK.

---

## 6. Alignment with M2b `pixel_source`

M2b §15a proposes `corrections.proposed.pixel_source ∈ {paired_jpeg, raw_thumb,
raw_postprocess}`, decided at measure time. With derive-at-ingest, corrections no
longer decides anything — it reads `proj_photos.jpeg_path` and the source is already
recorded. **One vocabulary, one writer:**

- `photo.ingested.jpeg_source` / `proj_photos.jpeg_source` ∈
  `{"camera", "raw_preview", "raw_postprocess"}` — set once, at ingest.
- M2b's `pixel_source` becomes a straight copy of that column, renaming
  `paired_jpeg → camera` and `raw_thumb → raw_preview`.

M2b is an unmerged design, so this rename costs a doc edit (slice 3). The point M2b
made stands and is inherited: an exposure statistic is not comparable across sources,
so the feedback ledger must record which one produced the pixels.

---

## 7. EXIF

**`_extract_exif()` is unchanged and gets no branch.** It opens whatever
`jpeg_path` points at. For a derived photo that is the preview file we just wrote.

What that means, honestly:

- **If the embedded preview carries an APP1 EXIF block** (most full-size camera
  previews do — *unverified for this body*), the recorded fields are identical to
  what a paired JPEG would have given: `DateTimeOriginal`, `Model`, `LensModel`,
  `ISOSpeedRatings`, plus `width`/`height` of the preview.
- **If it does not**, `exif` is `{width, height}` only. Lost: capture time, body,
  lens, ISO.
- **rawpy cannot fill the gap.** It exposes `sizes`, `camera_whitebalance`,
  `black_level`, `color_desc` — sensor/decode metadata, not EXIF tags. There is no
  "read it from the RAW via rawpy" option for `Model`/`DateTimeOriginal`.
- **No EXIF parser is being added.** `exifread`/`piexif` would be a new dependency
  (§8.2) to populate a column that **nothing currently reads**: `exif_json` is
  written at `projections.py:70` and consumed by zero call sites in `src/` or
  `frontend/`. Adding a dependency to enrich dead data is the wrong order of
  operations. When a consumer appears (catalog-gap scoring is the likely one), that
  PR proposes the parser with a measured need.
- The slice-1 probe prints the EXIF keys actually recovered from real files, so this
  goes from unverified to measured before slice 2 merges.

`width`/`height` in `exif` are the *preview's* dimensions (8192×5464), not the
sensor's (8480×5650) — a 0.1% aspect difference and the honest dimensions of the file
we hand downstream, which is what consumer 4 measures anyway.

---

## 8. The reverse case (JPEG with no RAW) — deliberately untouched

`ingest.py:114-127` still emits `photo.unpaired{reason:"missing_raw"}`. Leaving it
alone is not laziness by omission; it is the only coherent option:

1. **Identity.** `photo_id` *is* the RAW's sha256 (`ingest.py:164`). A JPEG-only
   photo has no identity under the current scheme; giving it one means two identity
   schemes in one table.
2. **The print master.** PRD §5.1: "The RAW is hash-tracked and carried forward as
   the print master." `dispatch.py:51` sends `raw_path` to Lightroom, and
   `JobFile.lua:41` requires it. A JPEG-only photo cannot be dispatched to Gate 2,
   so a hero-mode JPEG-only ingest would create a photo that can pass Gate 1 and then
   dead-end.
3. **There is already a path for it.** A JPEG that must reach the shop goes in the
   **landing folder** — decision 33 gives manual drops with unknown `photo_id` full
   mockup sets, and decision 6 says landing files are never re-scored. That is the
   catalog-backfill path, and it works today.
4. It is not the operator's problem. The reported problem is RAW-only folders.

Symmetry is not a requirement. The two cases are asymmetric because the RAW is the
master and the JPEG is a derivative — which is precisely why one can be manufactured
from the other and not the reverse.

---

## 9. Decode ladder and failure modes

`src/shopsteward/editing/rawpreview.py` — one public function, `import rawpy` inside
it (contains import cost, keeps the failure legible, gives tests a seam):

```python
def extract_preview_jpeg(path: Path, cfg: dict) -> tuple[bytes, str]:
    """Returns (jpeg_bytes, source) where source is "raw_preview" | "raw_postprocess".
    Raises PreviewUnavailable(reason, error) — never returns None, never guesses."""
```

| Rung | Condition | Action | Cost |
|---|---|---|---|
| 0 | `rawpy.imread()` raises (`LibRawFileUnsupportedError`, `LibRawIOError`, corrupt file) | **stop** → `raw_undecodable` | — |
| 1 | `extract_thumb()` → `ThumbFormat.JPEG`, Pillow-openable, `max(size) ≥ min_preview_long_edge_px` | write bytes **verbatim** | **9 ms** |
| 2 | `ThumbFormat.BITMAP` (ndarray) | Pillow-encode at `derived_jpeg_quality`, same size gate | ~50 ms |
| 3 | `LibRawNoThumbnailError`, `LibRawUnsupportedThumbnailError` (the H.265-preview case), Pillow cannot open the bytes, **or** the preview is below the size gate | → rung 4 | — |
| 4 | `fallback_postprocess` true for this mode | `postprocess(use_camera_wb=True, output_bps=8)` → Pillow-encode; `source="raw_postprocess"` | **1.14 s** |
| 4′ | `fallback_postprocess` false (mass default) | **stop** → `raw_preview_unavailable` | — |
| 5 | `postprocess()` itself raises | **stop** → `raw_undecodable` with `error:{code,message}` | — |

**The ladder stops at rung 5. There is no rung 6.** No third-party preview
extractor, no `dcraw` shell-out, no "write a placeholder image". A RAW we cannot
decode does not become a photo, and says so in the event log.

**Mass mode refuses rung 4 by default.** 2,000 × 1.14 s = 38 min inside a CLI
invocation with no progress bar, to produce files that today have **zero readers** —
mass photos are `queued_for_edit`, never scored, never in Gate 1; the only future
reader is M2b corrections. Hero mode allows rung 4: one frame, 1.14 s, and every
consumer in §1 is live. Both are config, per-mode, flippable.

**The size gate is not cosmetic.** `technical.min_long_edge_px` is **4000** in
`config/defaults/tuning_profile.json:20`, and `technical.py:60-64` caps any photo
below it at score 40 — a hard "never reaches Gate 1". That guard exists to measure
the *RAW's* print headroom; feeding it an undersized preview would silently kill
good photos. So `min_preview_long_edge_px` defaults to **4000 in hero, mirroring
that guard**, and a preview below it falls to demosaic rather than producing a
quietly-doomed photo. The editing module cannot import the pipeline's tuning profile
(boundary), so the number is duplicated in `editing.json` with a comment saying it
mirrors `tuning_profile.scoring.technical.min_long_edge_px` and must be kept in sync
by hand. Mass overrides it to 1600 (M2b's `analysis_long_edge_px`), since no
resolution guard runs there.

`postprocess()` is a deterministic demosaic, not a model — same class as
`cv2.imread`. "AI never touches the photograph" is not engaged (§11).

---

## 10. Dependency, boundaries, config

**rawpy** (operator-approved 2026-08-03, M2b §15a decision 10 reversed).

- **Offline.** `rawpy` is a Cython wrapper around bundled LibRaw 0.22.1. No sockets,
  no credentials, no telemetry. **PRD §11's zero-credentials standalone property for
  the editing module is preserved** — this is the only property that could have been
  broken, and it is not.
- **Declared** in `[project].dependencies` in `pyproject.toml`, beside `pillow` and
  `opencv-python-headless`. Not a dev dep, not an extra: PRD decision 4 is one
  package with one `shopsteward edit` entry point, so the editing module's runtime
  deps are the project's runtime deps.
- **Import-linter: zero contract changes.** All three contracts in `pyproject.toml`
  enumerate `shopsteward.*` modules only; a third-party import is invisible to them.
  `rawpreview.py` imports `rawpy`, `PIL`, `pathlib` — nothing from `pipeline`,
  `adapters.etsy`, or any other forbidden module.
- **No adapter Protocol, deliberately.** CLAUDE.md's rule is that every *external
  system* sits behind an adapter. LibRaw is a local codec library in the same class
  as Pillow and OpenCV, which `pipeline/scorers/technical.py:4` and
  `pipeline/landing.py:9` already import directly. An adapter here would be an
  interface with one implementation — the exact thing decision 51 settled for the
  corrections module. The test seam is the module function, not a Protocol (§14).
- **Windows wheel install must be verified before slice 2.** That is slice 1's job.

**Config** — `config/defaults/editing.json` gains one block. No new config file, no
new seed event: `editing.json` is loaded file-direct by `config.load_editing_defaults()`
(a pre-existing local deviation from DB-seeding that this change follows rather than
forks).

```json
{ "naming_template": "{event}-{seq:04}",
  "event_output_root": "data/deliveries",
  "jpeg_quality": 92,
  "ingest": {
    "derive_from_raw": true,
    "min_preview_long_edge_px": 4000,
    "_comment_min_preview": "mirrors tuning_profile.scoring.technical.min_long_edge_px; keep in sync by hand (module boundary forbids reading it)",
    "fallback_postprocess": true,
    "derived_jpeg_quality": 95,
    "modes": { "hero": {},
               "mass": { "min_preview_long_edge_px": 1600,
                         "fallback_postprocess": false } } } }
```

`ingest_folder()` gains `ingest_cfg: dict | None = None`, defaulting to
`load_editing_defaults()["ingest"]` with the mode block merged over the base — the
same `modes` override shape as `corrections.json`. Zero thresholds in Python.

**Files touched:**

```
src/shopsteward/editing/rawpreview.py     NEW — extract_preview_jpeg() + PreviewUnavailable
src/shopsteward/editing/ingest.py         RAW-only branch, jpeg_source, derived counter, dedupe repair
src/shopsteward/editing/models.py         PhotoPair.jpeg_source; IngestReport.derived
src/shopsteward/editing/projections.py    proj_photos.jpeg_source; proj_ingest_jobs.derived
src/shopsteward/editing/cli.py            + `shopsteward edit probe-raw <path>`
src/shopsteward/settings.py               + derived_dir()
config/defaults/editing.json              + "ingest" block
pyproject.toml                            + rawpy
tests/editing/test_ingest.py              + RAW-only cases (stubbed seam)
tests/editing/test_rawpreview.py          NEW — ladder + env-gated real-file test
docs/PRD_v2.1.md                          §5.1 + decision 17 amendment; §10 M2a row; §13 57-63
docs/designs/2026-08-03-corrections-proposer.md   §15a/§15b pixel_source alignment
```

---

## 11. Guardrail impact and rollback

| Guardrail | Impact |
|---|---|
| Monolithic core / pluggable adapters | **None.** LibRaw is a local codec, not an external system; no adapter, per decision 51's precedent. |
| Editing-module boundary | **None.** No new `shopsteward.*` import; no import-linter change; the zero-credentials property (PRD §11) is preserved. |
| Landing-folder handoff | **None.** `data/derived/` is not the landing folder and nothing writes across the boundary. |
| Event-sourced SQLite | Append-only. **No new event type**; one new payload field, two new `reason` values, one new counter. Nothing updated, nothing deleted; both projections are drop-and-rebuild with backward-compatible `.get()` defaults. |
| Configuration over code | Every threshold and per-mode override in `editing.json`. Zero magic numbers in Python. |
| POD-first listing creation | Untouched. |
| AI never touches the photograph | **Not engaged.** `extract_thumb()` copies bytes the camera wrote; `postprocess()` is a deterministic demosaic. No model, no generative step, and the derived file is never the sold file (§12). |
| `user_id` on every major table | Unchanged in both projections. |
| Public repo | No image is committed; `.gitignore` already blocks `data/` and `*.cr3`. Tests synthesize (§14). |
| Three gates | **No new touchpoint.** RAW-only photos enter the *existing* Gate 1 queue. |
| New dependency | **rawpy — operator-approved 2026-08-03; re-confirmed in writing (§17.2).** |

**Rollback — config only, no code revert, no event deletion:**

- `ingest.derive_from_raw: false` → RAW-only files emit `photo.unpaired{missing_jpeg}`
  exactly as today, byte-identical behaviour. **This is the kill switch.**
- Per-mode: `modes.mass.fallback_postprocess` / `min_preview_long_edge_px` tune the
  ladder without disabling anything.
- `data/derived/` can be deleted at any time; already-appended events stay (append-only)
  and the projection simply points at absent files until the next ingest run.
- **Trigger:** if derived-preview photos score systematically differently from
  camera-JPEG photos on the technical scorer (compare `proj_scores.technical` grouped
  by `proj_photos.jpeg_source` after the first mixed batch of ≥50 photos), the size
  gate or the preview premise is wrong — flip `derive_from_raw: false` and shoot
  RAW+JPEG until it is understood.
- **Pre-code trigger:** if the slice-1 probe shows the operator's previews carry no
  EXIF *and* land below 4000 px, the design's premise is broken and slice 2 is not
  written as specced.

---

## 12. Is a derived JPEG ever the sellable file? **No.**

**Position: a camera-preview-derived JPEG may never be the sellable file of an Etsy
listing, and the architecture already makes it structurally impossible.**

M5a resolves the sellable file from `proj_landing_files.path`
(`pipeline/listings/images.py:36-59`), and its `derived_jpeg` source is something
different in kind: a deterministic Pillow re-encode *of a Gate 2 export* done only
when the original is a TIFF or exceeds Etsy's 20 MB limit. Our derived file is the
camera's own preview of an unfinished RAW: it carries the in-camera picture style,
has never been through Gate 2, and reflects none of the operator's finishing. It is
an *analysis and review* surface, not a product.

The guard is the landing-folder rule, not a new check. `data/derived/` is not
`data/landing/`; `scan_landing()` only ever iterates `landing_dir()`; nothing in
`mockups/` or `listings/` reads `proj_photos`. No code change is needed to enforce
this and none is proposed.

The one way it could happen is the operator manually dragging a derived file into the
landing folder. Filenames are 64-hex `<photo_id>.jpg`, which look nothing like a
Lightroom export and match no `base_name` in `landing.py:53-57` (so such a file would
land as an unmatched manual drop, visibly). That is judged **not load-bearing enough
to build a check for**, but the product rule is worth ratifying in the decision log —
hence §17.5, a yes/no confirmation rather than a code guard.

---

## 13. Milestone placement — its own row, **M2a**, sequenced before M2b

Not an M2 amendment: M2 and M3 are shipped and merged, and CLAUDE.md requires PRs
scoped to one milestone. Not part of M2b: this is *strictly larger* than corrections
and independent of it — it makes RAW-only photos exist to scoring, Gate 1, dispatch
and the landing matcher whether or not a corrections proposer is ever built, and it
amends PRD §5.1/decision 17, which is not M2b's business. It also carries its own
§8.2 gates (a runtime dependency, an event-payload change, a PRD amendment) that
would otherwise be buried inside M2b's first PR.

M2b **depends on it**: per M2b §15a, corrections in a RAW-only folder has no pixels
to measure, and §15b.1(a) is exactly this design. `a`-before-`b` matches the M5a/M5b
precedent, where the letters encode build order.

**Proposed PRD §10 row:**

> **M2a** — RAW-only ingest: derive the pixel companion from the RAW's embedded
> camera preview at ingest (rawpy/LibRaw, offline), `jpeg_source` on every photo,
> decode-failure ladder with per-mode fallback policy. *1 weekend.* Folders of CR3s
> with no paired JPEG enter scoring, Gate 1, and editing unchanged.

---

## 14. Test plan — zero network, zero committed images

The repo has no photo fixtures and must not gain any; `.gitignore` blocks `*.cr3`
outright, so a committed RAW is impossible by policy as well as by rule. And unlike a
JPEG, **a valid CR3 cannot be synthesized with Pillow**. So the honest split:

**Fakeable at the `rawpreview` boundary — everything except the decoder itself.**
`ingest.py` calls exactly one function, `rawpreview.extract_preview_jpeg(path, cfg)`.
Tests monkeypatch that name (no Protocol, no factory — one module attribute) with a
stub returning `(PIL-made JPEG bytes, "raw_preview")`, or raising
`PreviewUnavailable`. That covers: the RAW-only branch, derived-file path/naming,
atomic write, `jpeg_source` on the event and in the projection, the `derived`
counter, EXIF extraction from the derived file, dedupe on re-run, the missing-file
repair path, both new `unpaired` reasons, per-mode config merge, and the
`derive_from_raw: false` rollback. That is essentially all of the new logic.

**Fakeable without a RAW but needing real rawpy** — the exception-type contract.
`tests/editing/test_rawpreview.py` imports the real `rawpy.LibRawNoThumbnailError`
and `rawpy.LibRawUnsupportedThumbnailError` and raises them from a patched
`rawpy.imread`, asserting the ladder routes each to the right rung. No file needed;
this catches the "we caught the wrong exception class" bug, which is the most likely
real failure.

**Only verifiable by hand — the decoder against a real CR3.** That rawpy installs on
Windows, opens a CR3, returns `ThumbFormat.JPEG`, and yields Pillow-openable bytes
above the size gate. Two instruments, both offline, neither committing an image:

1. **`shopsteward edit probe-raw <path>`** (slice 1) — reads a folder or file, prints
   per RAW: rung taken, thumb format, pixel dimensions, **byte size**, elapsed ms,
   and the EXIF keys recovered. Writes nothing, appends no events, touches no DB.
   This is the operator's verification run, the source of the disk-cost number §3
   leaves unmeasured, and the answer to the §7 EXIF uncertainty.
2. **`SHOPSTEWARD_RAW_SAMPLE=<path to a CR3 outside the repo>`** — a single
   `pytest.mark.skipif`-gated test that runs the real decode and asserts format,
   dimensions ≥ gate, and Pillow-openability. Never runs in CI, never sees a
   committed file, gives the operator one command to re-verify after a rawpy upgrade.

**Smallest test that proves it works** —
`tests/editing/test_ingest.py::test_raw_only_folder_ingests_with_derived_preview`:
a folder with one stand-in `.CR3` (bytes, as `test_ingest.py:23` already does) and
**no** JPEG, `extract_preview_jpeg` stubbed; assert exactly one `photo.ingested` with
`jpeg_source == "raw_preview"`, `jpeg_path` under `derived_dir()` and existing on
disk, `report.derived == 1`, and **zero** `photo.unpaired` events. If the branch, the
write, the event field, the config plumbing, or the counter breaks, that one test
fails.

**E2E** (extends `tests/editing/test_e2e_mass_mode.py` and adds a hero counterpart):
RAW-only mass ingest of 3 stand-in CR3s with `fallback_postprocess: false` and a stub
that raises `PreviewUnavailable` on one of them → 2 ingested + 1
`photo.unpaired{raw_preview_unavailable}` → dispatch → `FakeBridge.consume_all()` →
`proj_photos` `edited`. Re-run → `duplicates == 3`, no new `photo.ingested`, no
second derived write (mtime unchanged). Delete one derived file, re-run → file
restored, still no new events. Hero: RAW-only ingest → `run_scoring` with the
fixture vision adapter reads the derived JPEG through `technical` and `commercial`
→ `photo.scored` → Gate 1 preview endpoint returns 200. Assert zero network.

---

## 15. Implementation slices (dependency order)

| # | Slice | Size | Mergeable independently |
|---|---|---|---|
| **1** | **FIRST PR — the §8.2 gate.** `rawpy` in `pyproject.toml`; `editing/rawpreview.py` (full ladder, `PreviewUnavailable`); `ingest` block in `editing.json`; `shopsteward edit probe-raw <path>`; `tests/editing/test_rawpreview.py` incl. the env-gated real-file test. **`ingest.py` is not touched.** Operator runs `probe-raw` on real folders and reports back: rung taken, dimensions, bytes/frame, EXIF keys. | 1 evening | Yes — ships the verification instrument and proves the Windows wheel before any behaviour changes. |
| **2** | The ingest change: RAW-only branch, `settings.derived_dir()`, atomic derived write, `jpeg_source` on `photo.ingested` + `proj_photos`, `derived` counter on `ingest.completed` + `IngestReport` + `proj_ingest_jobs`, new `unpaired` reasons, dedupe-branch repair, per-mode config merge, `derive_from_raw` kill switch. Full stubbed test suite + mass E2E. | 1 weekend | Yes — RAW-only folders ingest and dispatch end to end. |
| **3** | Downstream proof + surfacing + docs: hero E2E through scoring and the Gate 1 preview endpoint on a derived photo; `derived` and `jpeg_source` counts in `shopsteward edit status` and `GET /api/editing/jobs`; PRD §5.1 / decision 17 amendment, §10 M2a row, §13 57–63; M2b §15a/§15b `pixel_source` → `jpeg_source` alignment. | 1 evening | Yes. |

Slice 1 is where the operator says go/no-go with measurements in hand rather than
with reasoning in hand.

---

## 16. PRD §13 decision-log amendment (candidates 57–63)

```
M2a design (2026-08-03; normative spec at
docs/designs/2026-08-03-raw-only-ingest.md):

57. RAW-only folders ingest. A RAW with no paired JPEG is no longer skipped;
    it becomes a normal photo whose pixel companion is DERIVED from the RAW's
    embedded camera preview at ingest time. This is milestone M2a, its own
    PRD §10 row, sequenced before M2b (which depends on it: corrections has no
    pixels to measure in a RAW-only folder). It AMENDS §5.1 and decision 17 —
    the paired JPEG is now the preferred pixel source, not the only one — and
    amends decision 17's "avoids a CR3-parsing dependency" rationale, which
    the rawpy approval of 2026-08-03 superseded.
58. Derived AT INGEST, never lazily. All four live consumers of
    proj_photos.jpeg_path (technical scorer, commercial/vision prep, Gate 1
    preview endpoint, M2b corrections) take a filesystem PATH, not bytes, so
    lazy derivation would write the same file later from four places. The
    invariant is: every ingested photo has exactly one readable JPEG on disk;
    jpeg_path is never null or empty and jpeg_source is never "none".
    Measured cost: 9 ms/frame via extract_thumb() at 8192x5464, against
    1.14 s for a full postprocess() demosaic (~125x).
59. Derived previews live in data/derived/<photo_id>.jpg (photo_id = the RAW's
    sha256), env-overridable via SHOPSTEWARD_DERIVED_DIR, owned solely by
    editing/ingest.py, gitignored. It is a CACHE: safe to delete at any time,
    regenerated by re-running `shopsteward ingest <folder>` (the dedupe branch
    silently re-derives a missing derived file and appends no event, because
    the same RAW always yields the same bytes at the same path). There is no
    prune command.
60. A camera-preview-derived JPEG is NEVER the sellable file of a listing. It
    carries the in-camera picture style and has never been through Gate 2,
    which makes it categorically different from M5a's derived_jpeg (a
    deterministic re-encode of a Gate 2 export). Enforcement is structural —
    the sellable file resolves from proj_landing_files and data/derived/ is
    not the landing folder — so no new check is built.
61. Event model: NO new event type. photo.ingested gains
    jpeg_source in {"camera","raw_preview","raw_postprocess"} (projections
    default absent values to "camera"); ingest.completed and IngestReport gain
    a `derived` counter; photo.unpaired gains reasons raw_preview_unavailable
    and raw_undecodable, and missing_jpeg is emitted only when the feature is
    disabled. A prior photo.unpaired does NOT block a later photo.ingested —
    ingest dedupes on photo.ingested.raw_sha256 only — so re-running ingest
    over the folder IS the entire backfill mechanism. No projection
    reconciliation is needed: rebuild_editing() never folded photo.unpaired
    (it carries a path, not a photo_id), so the two events coexist as accurate
    history and produce exactly one proj_photos row. M2b's
    corrections.proposed.pixel_source becomes a copy of jpeg_source, adopting
    this vocabulary (raw_thumb -> raw_preview, paired_jpeg -> camera).
62. Decode ladder, stopping at rung 5 with no further fallback: rawpy.imread
    fails -> raw_undecodable; extract_thumb JPEG above the size gate -> bytes
    written verbatim; BITMAP -> Pillow-encoded; no/unsupported (H.265)/
    undersized preview -> postprocess(use_camera_wb=True) in HERO only;
    MASS refuses the demosaic by default (2,000 x 1.14 s = 38 min for files
    with no reader today) and records raw_preview_unavailable. The hero size
    gate (min_preview_long_edge_px, default 4000) deliberately MIRRORS
    tuning_profile.scoring.technical.min_long_edge_px, kept in sync by hand
    because the module boundary forbids reading it; an undersized preview
    would otherwise trip the resolution guard and cap a good photo at 40. All
    thresholds and per-mode overrides live in config/defaults/editing.json;
    ingest.derive_from_raw:false restores pre-M2a behaviour exactly and is the
    kill switch.
63. rawpy/LibRaw is a runtime dependency of the shopsteward package. It is
    fully OFFLINE, so the editing module keeps PRD §11's zero-credentials
    standalone property. It gets NO adapter Protocol: LibRaw is a local codec
    in the same class as Pillow and OpenCV, which editing and pipeline already
    import directly; the adapter rule targets external SYSTEMS (decision 51's
    precedent — no interface with one implementation). No import-linter
    contract changes. EXIF continues to be read from whatever jpeg_path points
    at, with no branch; if an embedded preview carries no APP1 block the record
    is {width,height} only (rawpy exposes no EXIF tags), and NO EXIF parser is
    added because nothing reads exif_json today. JPEG-without-RAW stays
    skipped: photo identity IS the RAW sha256, the RAW is the print master
    Lightroom needs, and the landing folder is already the path for
    JPEG-only images (decisions 6 and 33).
```

---

## 17. OPERATOR DECISIONS REQUIRED

| # | Question | Answer |
|---|---|---|
| 1 | Create milestone **M2a — RAW-only ingest**, sequenced before M2b (§13)? | Yes / No |
| 2 | Confirm in writing: **rawpy** as a runtime dependency in `[project].dependencies` (§8.2 new dependency; verbally approved 2026-08-03) | Yes / No |
| 3 | **Derive at ingest** (M2b §15b.1 option **a**) rather than lazily on first use (§3)? | Yes / No |
| 4 | Derived files at **`data/derived/<photo_id>.jpg`**, env var `SHOPSTEWARD_DERIVED_DIR` (§4)? | Yes / No |
| 5 | Ratify: **a camera-preview-derived JPEG may never be the sellable file** of a listing (§12)? | Yes / No |
| 6 | **Mass mode refuses the `postprocess()` fallback** by default — RAW-only mass frames with no usable preview are skipped and logged (§9)? | Yes / No |
| 7 | `min_preview_long_edge_px` **hero** value (proposed 4000, mirroring the technical resolution guard) | single value |
| 8 | `min_preview_long_edge_px` **mass** value (proposed 1600) | single value |
| 9 | `derive_from_raw` on first merge: **true** (proposed — unlike M2b there is no useful dark mode; false = the feature does nothing) | true / false |
| 10 | **JPEG-without-RAW stays skipped**; landing-folder manual drop remains its path (§8)? | Yes / No |
| 11 | Accept the disk cost of one derived JPEG per RAW-only frame, with the **exact bytes/frame measured by slice 1's `probe-raw` before slice 2 merges** (§3)? | Yes / No |
| 12 | Amend PRD **§5.1 and decision 17** to record that the paired JPEG is the preferred, not the only, pixel source (§16 candidate 57)? | Yes / No |
| 13 | Amend `docs/designs/2026-08-03-corrections-proposer.md` §15a/§15b so `pixel_source` becomes a copy of `jpeg_source` with the unified vocabulary (§6)? | Yes / No |

---

## 18. Non-goals (explicit)

Ingesting JPEG-without-RAW (§8); a `RawDecoderAdapter` Protocol; RAW formats beyond
`.CR3` (`RAW_SUFFIXES` stays `{".cr3"}` in Python until the operator has a second
body — rawpy handles NEF/ARW/DNG the day it matters); an EXIF parser for RAW files
(§7); XMP sidecar reading or writing; any use of the derived JPEG as a print,
delivery, or sellable file (§12); re-deriving when the *camera* JPEG is deleted
(`photo.jpeg_resourced` is specced in one line, not built — §4); a derived-file
prune/GC command; user-partitioned derived storage (multi-tenant follow-up);
back-filling `photo.unpaired` history with a repair script (re-running ingest is the
mechanism — §5); progress bars for long mass ingests; any change to scoring,
Gate 1, dispatch, the landing folder, mockups, or listings.

---

## 19. Rejected alternatives

- **Lazy derivation on first use.** All four live pixel consumers take a path, not
  bytes (§1), so the file gets written anyway — later, from an HTTP request thread,
  in four modules instead of one — and `rawpy` leaks into `pipeline/`, which has no
  other reason to know RAW exists. The write is deferred and duplicated, not avoided.
- **Corrections reads RAWs directly, ingest untouched** (M2b §15b.1 option b). The
  photos still never reach scoring, Gate 1, or dispatch, so it fixes nothing the
  operator would notice, and it puts a decoder in the module that is *not* the one
  that owns photo identity.
- **Leave as-is; always shoot RAW+JPEG** (option c). A camera-setting workaround for a
  software defect, and it does nothing for the folders that already exist.
- **`postprocess()` for every frame instead of `extract_thumb()`.** 125× slower;
  38 min for a 2,000-frame event against 18 s. The measured preview is 8192×5464 —
  near-full-resolution — so the fast path concedes no quality that matters at a
  1600 px analysis size or a 4000 px resolution guard.
- **A `RawDecoderAdapter` Protocol with one implementation.** LibRaw is a local
  codec, not an external system; `cv2` and `PIL` are already imported directly across
  `editing/` and `pipeline/`. Decision 51 settled this class of question. The test
  seam is a module function (§14), which is strictly less machinery.
- **A new `photo.raw_only_ingested` event type.** Two ingest event types, two folds,
  two dedupe paths, for one fact that is a field. M2b §12's "one event, not three"
  reasoning applies unchanged.
- **A migration/backfill command that rewrites or supersedes old `photo.unpaired`
  rows.** Events are immutable; and it is unnecessary — the dedupe path already
  ignores `photo.unpaired` entirely, so re-running ingest picks the RAWs up (§5).
- **Making `proj_photos.jpeg_path` nullable so mass mode can skip derivation and
  save 8–16 GB.** Reintroduces the missing-pixels branch in every consumer — the
  precise cost that eager derivation buys off — to save regenerable cache on a
  machine that already stores the masters.
- **Downscaling the derived preview to save disk.** It would trip
  `technical.min_long_edge_px = 4000` and silently cap every RAW-only photo at score
  40, permanently below the Gate 1 threshold of 60. A resolution guard measuring the
  wrong file is worse than a big file.
- **Adding `exifread`/`piexif` to recover capture metadata the preview may lack.** A
  new dependency to populate a column no code reads (`exif_json` has zero consumers).
  Propose it when a consumer exists.
- **A "sellable file may be a derived preview if no Gate 2 export exists" fallback.**
  It would sell an unfinished frame with the camera's picture style and skip Gate 2 —
  a product regression dressed as a convenience.
- **Writing derived previews into the landing folder** so they are visible to the
  pipeline. Violates the landing-folder rule outright (§12) and would make every
  RAW-only ingest look like a Gate 2 export.
