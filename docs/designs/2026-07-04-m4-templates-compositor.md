# M4 Design — Staging Template Library + Mockup Compositor

*Architect output 2026-07-04, approved by operator same day. Ambiguity
resolutions: gallery-wall v1 = varied deterministic crops of the one photo
(companion_photos param plumbed for M5); manual-drop landing files DO get
mockup sets (backfill path); acrylic gloss overlay approved as deterministic
mockup compositing (the sold artwork file is never modified). PRD §13
decisions 28–31 + these = 32–34.*

Extends house patterns: append-only events, drop-and-rebuild module-local
projections, on-demand scans, Typer sub-apps, APIRouter mounted by top-level
api.py, config loaded direct from file (load_editing_defaults precedent).
Consumes proj_landing_files (status valid); produces mockup JPEGs + events
for M5. No Etsy/POD, no AI adapter, no new gates.

## 1. File map

```
src/shopsteward/mockups/__init__.py
src/shopsteward/mockups/models.py       Pydantic v2: StagingTemplate, TemplateRegion, MockupConfig,
                                        MockupJobResult, TemplateReport, MockupRecord.
src/shopsteward/mockups/config.py       MOCKUP_DEFAULTS_PATH + load_mockup_defaults() + config_hash().
src/shopsteward/mockups/templates.py    Registry scan (mirrors landing.py): merge
                                        config/defaults/staging_templates/ + data/staging_templates/,
                                        validate sidecars, compute avg_hue, emit stagingtemplate.* events.
                                        write_sidecar() for the annotate endpoint.
src/shopsteward/mockups/compositor.py   PURE functions, no DB/no events: composite_print(), light_match(),
                                        draw_shadow(), draw_frame(), draw_mat(). cv2+Pillow only.
src/shopsteward/mockups/intents.py      One render fn per intent; template-backed intents call compositor;
                                        synthetic intents are pure Pillow/cv2.
src/shopsteward/mockups/selection.py    select_templates(photo_stats, templates, intent, n) — pure.
src/shopsteward/mockups/jobs.py         Orchestrator: eligible landing files -> select -> render -> write
                                        data/mockups/... -> mockup.generated + mockupset.completed.
src/shopsteward/mockups/projections.py  proj_staging_templates, proj_mockups, proj_mockup_sets;
                                        rebuild_mockups().
src/shopsteward/mockups/api.py          APIRouter prefix /api/pipeline (mockups+templates routes).
src/shopsteward/mockups/cli.py          `shopsteward mockups` Typer sub-app.
config/defaults/mockups.json            Intent/render defaults (schema §2).
config/defaults/staging_templates/      4 committed synthetic placeholder JPEGs + sidecars.
scripts/make_placeholder_templates.py   Committed generator; outputs committed.
frontend/src/pages/Templates.tsx        Registry list + click-4-corners annotator.
frontend/src/pages/Mockups.tsx          Per-photo mockup-set gallery.
settings.py                             + mockups_dir() (SHOPSTEWARD_MOCKUPS_DIR, default data/mockups)
                                        + operator_templates_dir() (default data/staging_templates).
```

import-linter: mockups may import pipeline (one-way); add shopsteward.mockups
to forbidden_modules of the layering contract (core/editing/pipeline/adapters
must not import mockups) — i.e. extend the existing contract's source list
with a new contract "mockups is imported by no lower layer": sources core,
editing, pipeline, adapters.* → forbidden shopsteward.mockups.

## 2. Schemas

Sidecar `<image_stem>.template.json`, schema "shopsteward.stagingtemplate/1":

```json
{ "schema": "shopsteward.stagingtemplate/1",
  "template_id": "livingroom-warm-01",
  "room_type": "living_room",
  "style": "modern",
  "lighting": "warm_daylight",
  "orientation": "landscape",
  "regions": [
    { "kind": "wall_print",
      "quad": [[412.0, 180.0], [1180.0, 196.0], [1174.0, 720.0], [418.0, 704.0]],
      "region_width_inches": 36.0 } ],
  "tags": ["neutral_wall", "sofa", "plants"] }
```

Quad order TL,TR,BR,BL clockwise, px floats. Validation at LOAD (registry
scan): 4 points in bounds; convex (consistent cross-product signs); area ≥
1.5% of image, shortest edge ≥ 40px; region_width_inches ∈ [6,120] with
implied ppi (top edge px / inches) ∈ [10,300]; orientation ∈ {landscape,
portrait, square, any}; template_id unique across both dirs (collision →
both invalid, duplicate_id). Multi-region (2–6) = gallery wall.

config/defaults/mockups.json, schema "shopsteward.mockups/1", file-direct:

```json
{ "schema": "shopsteward.mockups/1",
  "intents": {
    "single":             {"enabled": true, "count": 2},
    "gallery_wall":       {"enabled": true, "count": 1},
    "framed_poster":      {"enabled": true, "count": 1},
    "canvas_edge":        {"enabled": true, "count": 1},
    "acrylic":            {"enabled": true, "count": 1},
    "digital_whatyougot": {"enabled": true, "count": 1} },
  "render": {
    "output_long_edge_px": 2400, "jpeg_quality": 90,
    "mat_fraction": 0.06, "mat_color": [246, 246, 244],
    "frame_width_inches": 0.75, "frame_color": [24, 22, 20],
    "canvas_wrap_depth_inches": 1.25,
    "shadow": {"offset_frac": 0.006, "blur_frac": 0.010, "opacity": 0.35, "angle_deg": 115},
    "light_match": {"enabled": true, "gain_min": 0.65, "gain_max": 1.15,
                    "wb_min": 0.90, "wb_max": 1.10} },
  "products": {
    "default_print_widths_inches": {"landscape": 24, "portrait": 18, "square": 20} },
  "whatyougot": {
    "sizes": ["4x5", "5x7", "8x10", "11x14", "16x20", "A2", "A3", "A4"],
    "formats": ["JPEG 300 DPI", "sRGB"],
    "headline": "Instant Digital Download" },
  "listing_copy": {
    "ai_disclosure_line": "Room scenes are AI-generated staging mockups. The photograph itself is the artist's original work and is never AI-generated or AI-edited." } }
```

config_hash() = sha256 of canonical json.dumps(sort_keys, compact).

## 3. Events, projections, idempotency

| Event | Payload |
|---|---|
| `stagingtemplate.registered` | `{template_id, image_path, sidecar_path, sidecar_hash, image_hash, room_type, style, lighting, orientation, region_count, region_width_inches[], tags, avg_hue, source:"defaults"\|"operator"}` |
| `stagingtemplate.updated` | same shape, when sidecar_hash or image_hash changed |
| `stagingtemplate.invalid` | `{template_id\|null, sidecar_path, sidecar_hash, reason}` |
| `mockup.generated` | `{photo_id\|null, landing_file_id, set_key, intent, template_id\|null, path, params:{quad_index?, print_w_in, print_h_in, mat_frac, gain, wb, out_px}}` |
| `mockupset.completed` | `{photo_id\|null, landing_file_id, set_key, count, config_hash, template_library_hash}` |

Registry: sidecars **file-authoritative** + scan-emitted events for audit
(which template revision produced which mockup). Scan idempotent by
sidecar_hash+image_hash, runs at start of every `mockups run` and on
POST /templates/scan.

Projections: proj_staging_templates(user_id, template_id PK, image_path,
sidecar_path, sidecar_hash, room_type, style, lighting, orientation,
region_count, avg_hue, tags_json, source, status, reason);
proj_mockups(user_id, path PK, photo_id, landing_file_id, set_key, intent,
template_id, params_json, created_at); proj_mockup_sets(user_id, set_key PK,
photo_id, landing_file_id, count, completed_at).

**Idempotency:** set_key = sha256(landing_file_id | config_hash |
template_library_hash) where template_library_hash = sha256 over sorted
(template_id, sidecar_hash, image_hash) of valid templates. Skip landing
file when set_key exists; --force bypasses. Deterministic output paths
data/mockups/<photo_ref>/<intent>_<template_id|synthetic>[_r<i>].jpg
(photo_ref = photo_id or file-<landing_file_id[:12]> for manual drops) so
re-runs overwrite. Library change ⇒ regenerate (accepted; cheap).

## 4. Compositor + intents

composite_print(print_img, template_img, region, opts) — pure, no I/O.

1. Master prep (once/photo): Pillow open; 16-bit / non-sRGB ICC →
   ImageCms-convert sRGB 8-bit; downscale long edge ≤ 2× quad bbox.
2. Scale: ppi = |TR−TL| / region_width_inches; W_in from
   products.default_print_widths_inches[orientation]; H from print aspect.
   **Fit-within + mat, never fill/crop** — mat_fraction border in mat_color
   (skipped for canvas_edge/acrylic).
3. Target sub-quad: print+mat rect corners as normalized (u,v) in region
   rect, mapped into image space by bilinear interpolation over region quad.
4. Warp: getPerspectiveTransform + warpPerspective RGBA, INTER_AREA
   pre-downscale then LINEAR warp, alpha-composite.
5. Light match (pre-warp): neighborhood = dilate(quad, 4% width) − quad;
   luma gain clamp [gain_min, gain_max]; WB R/G, B/G multipliers clamp
   [wb_min, wb_max]; global off-switch.
6. Shadow (under print): quad silhouette offset offset_frac×W along
   angle_deg, blur blur_frac×W, × opacity.
7. Output: resize to output_long_edge_px, sRGB JPEG quality jpeg_quality.

| Intent | Template? | Render |
|---|---|---|
| single | 1-region | steps 1–7, mat + thin 0.25″ frame line |
| gallery_wall | multi-region | one photo, varied deterministic crops (largest region = full fit; others = center / rule-of-thirds crops at region aspect, fixed order). companion_photos param plumbed, unused v1. Crops are mockup-presentation only. |
| framed_poster | 1-region | single + draw_frame (frame_width_inches×ppi, frame_color, 2px bevel highlight top/left) |
| canvas_edge | synthetic | fixed ~12° yaw 3/4 view; side face = mirrored edge strip (wrap_depth×ppi) darkened 0.75 gradient; neutral backdrop + shadow |
| acrylic | synthetic | photo + 6px white polished edge, flat on neutral backdrop, large-offset soft shadow (1″ standoff cue), 8%-opacity diagonal gloss overlay (approved deterministic) |
| digital_whatyougot | none | 2400² Pillow panel: thumb, headline, size chips, format line, bundled default font |

## 5. Template selection

Eligible: orientation ∈ {photo's, any}; region_count fits intent. Rank by
circular hue distance photo avg_hue (computed once) vs template avg_hue
(computed at scan, stored). Diversity: greedy, skip room_type already used
anywhere in this photo's set, relax if exhausted. Ties: template_id lex.
Fewer than count → render what exists; zero → skip intent, count in report.

## 6. API / CLI / UI

POST /api/pipeline/mockups/run {photo_id?, force?} → {sets_completed,
mockups_written, skipped_idempotent, intents_skipped_no_template,
templates_invalid}; GET /mockups?photo_id=; GET /mockups/image?path=
(validated under mockups_dir, no traversal); GET /templates;
POST /templates/scan → TemplateReport; POST /templates/annotate
{image_path, sidecar} (image must be in a library dir; writes sidecar,
rescans, returns verdict); GET /templates/image?path= (validated).

CLI: `shopsteward mockups run [--photo-id X] [--force]`,
`mockups templates-scan`, `mockups status`.

UI: Templates.tsx (registry cards w/ invalid badges; annotator: click 4
corners TL→TR→BR→BL, live polygon, metadata form, multi-region add, implied-
ppi scale hint); Mockups.tsx (photo picker → set gallery by intent). Two new
App tabs.

## 7. Risks + E2E

1. Eyeballed region_width_inches → ppi validation band + annotator hint.
2. Light-match tinting → hard clamps, off-switch, params recorded.
3. Color management → mandatory sRGB conversion; AdobeRGB fixture in E2E.
4. Memory on big TIFFs → single prepped master reused; pre-warp downscale.
5. Library-change regeneration churn → accepted v1; noted fallback.

E2E (zero network): placeholders + 2-region gallery template + synthetic
3600×2400 AdobeRGB TIFF landing master → run → 6 intents ≥1 file each;
events match files; pixel sanity (region centroid ΔRGB > 25 vs template;
outside quad Δ ≤ 6; synthetic renders >5% non-backdrop; whatyougot has text
pixels); second run skipped_idempotent; --force regenerates; concave-quad
sidecar → stagingtemplate.invalid, excluded.

## 8. Rejected alternatives

DB-seeded template records (image can't live in DB; scan+events = same
audit, one truth source); DB-seeding mockups.json (editing.json file-direct
precedent); fill/crop-to-region (misrepresents artwork; fit+mat truthful);
rectify-then-inverse-warp (two resamplings, more blur).
