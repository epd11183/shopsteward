# External tools survey — culling, Lightroom automation, presets, POD

*2026-08-05. Survey only — nothing here is a proposal to build, and nothing here
has been adopted. Every dependency named would need operator approval per
CLAUDE.md.*

**How to read this.** Each entry ends in one of three verdicts:

- **BORROW** — the idea or the code is worth taking, license permits it.
- **REFERENCE** — read it when we build the equivalent; don't take a dependency.
- **CLOSED** — ruled out, with the reason. Don't re-research these.

Verified claims are marked. Anything reported-but-unverified says so, because
this session found the last survey's unverified claims to be where the cost was.

---

## 0. The three findings that matter

1. **`applyDevelopSettings` may not carry crop/geometry keys.** Adobe's own
   forums say the method is undocumented (introduced LR6/CC 2015) and that
   while it *may* apply crop settings, the documented SDK does not provide that
   capability. This is exactly the question TODO #3's `CropAngle` spike answers
   — the spike is now more likely to come back negative, so budget for the
   fallback (XMP sidecar, §3.2) rather than assuming levelling is buildable.
   ⚠ Reported from forum threads, **not verified**. The spike is still the proof.

2. **XMP sidecars are the industry-standard scorer→editor transport.** All four
   serious culling tools surveyed (pixcull, facet, PhotoSort, and LRTimelapse in
   the commercial world) write verdicts as XMP sidecars rather than driving
   Lightroom through a plugin. We built a Lua bridge instead. The bridge is
   right for *applying* settings; the sidecar is likely right for *ratings and
   picks*, and it costs one file write instead of a queue round-trip.

3. **Printify is architecturally viable where Printful is not.** Printful was
   closed because its API has zero product-creation endpoints. Printify's API
   documents both product creation and a **publish-to-sales-channel** flow with
   its own rate limit (200 requests / 30 min), with Etsy as a pre-built channel
   connected in Printify's UI — the same shape as Gelato. This does not reopen
   the supplier decision (Gelato is chosen, on cost and canvas availability),
   but it means **Printify, not Printful, is the fallback** if Gelato ever fails
   us. ⚠ Endpoint paths **not verified** — the docs site is JS-rendered and
   `WebFetch` only recovered the rate-limit table.

---

## 1. Culling and scoring

Our scorer sits behind `src/shopsteward/adapters/vision/`. Everything below is a
candidate *shape* for that adapter, not a replacement for it.

### 1.1 pixcull — **BORROW (the design, not the code)**
`ChrisChen667788/pixcull` · MIT · Python · 105★ · v2.45, 427 commits · active

Local-first culling for professional photographers. Scores on a **6-axis rubric
— technical, subject, composition, light, moment, aesthetic** — and exports
verdicts as XMP sidecars for Lightroom and Capture One. Multi-model fusion of
~8 ONNX models (U²-Net, ArcFace, CLIP ViT-L/14, wedding-moment and scene CNNs),
plus an optional LLM meta-judge on the operator's own key, plus a learned
rescorer trained on ~130 rows of the user's own corrections. Pipeline degrades
gracefully when a model is absent.

**Take three things:**
- The **6-axis rubric as our scoring-weights config shape.** Our weights live in
  the DB seeded from `config/defaults/`; six named axes is a better structure
  than a flat score, and it makes a rejection explainable at Gate 1
  ("composition 2/10") instead of just numeric.
- **XMP sidecar as verdict transport** — see §0 finding 2.
- **The learned rescorer pattern.** ~130 correction rows is a small enough
  target that our Gate 1 approve/reject stream would reach it in months, and it
  is the same feedback loop the PRD already wants for tuning profiles. Note
  they got personalisation out of ~130 rows, not thousands.

**Do not vendor it.** 427 commits including a Swift iOS app, a JS frontend, a
Lua plugin, video chains and speaker diarization. We would inherit all of it to
use one scoring path. If we ever want the models, they go behind
`adapters/vision/` as one more implementation of the existing interface, and
that is a new-dependency + AI-model-selection decision requiring approval.

### 1.2 facet — **REFERENCE**
`ncoevoet/facet` · MIT · Python + Angular · 184★ · active

The closest thing to a second opinion on pixcull. **9 dimensions**: aesthetic
quality, composition, face quality, eye sharpness, technical sharpness, colour,
exposure, subject saliency, dynamic range. Writes ratings, labels, keywords,
captions and face regions to XMP sidecars by default, originals untouched;
optionally embeds into JPEG/HEIC/TIFF/PNG/DNG. **Reads external edits back with
`--import-sidecars`** — round-trip with Lightroom, darktable, digiKam,
Capture One.

**Why it earns a line:** the round-trip. Our bridge is one-directional today
(we push settings, we read an export report). Facet's `--import-sidecars` is the
pattern for detecting that the operator overrode us at Gate 2 — which is
precisely the signal the corrections proposer (M2b) needs to learn from. Also
useful as a cross-check on pixcull's rubric: the two agree on
technical/composition/aesthetic/exposure and diverge on faces vs. moment. Our
subjects are wildlife and landscape, so **the face axes are dead weight for us**
and the moment axis matters only in mass mode.

Self-hosted app with a web gallery at `localhost:5000`, not a library.

### 1.3 PhotoSort — **REFERENCE (for the model shortlist)**
`duartebarbosadev/PhotoSort` · Apache-2.0 · Python · 19★ · 363 commits

Small but the most *legible* stack of the three, and Apache-2.0 is the friendliest
license here:

| Job | Model |
|---|---|
| Similarity / clustering | `facebook/dinov2-small` (or `-base`) |
| Aesthetic scoring | `cafeai/cafe_aesthetic` |
| Technical analysis | OpenCV cascades + MediaPipe Face Mesh |
| Rotation detection | fine-tuned EfficientNetV2, ONNX |
| Optional AI ratings | any OpenAI-compatible vision endpoint |

**Take:** the model shortlist as a starting point if we ever go local. It is far
lighter than pixcull's eight-model fusion, and `cafe_aesthetic` + DINOv2 is the
boring, well-trodden pair. **Also take the "pick best from a similarity cluster"
framing** — that is mass mode's actual job description (weddings, races, sports
produce bursts), and it is a different problem from hero-mode single-image
scoring. Handles RAW with a preview cache; persists ratings to XMP sidecars.

### 1.4 photocull-ai — **REFERENCE (only as a UI idea)**
`joneilcaoile/photocull-ai` · single HTML file, browser-local, zero server

10 analysis features, keyboard-driven workflow, 100% private. Not a fit for our
pipeline, but it is a working demonstration that a **whole culling UI fits in
one static file with keyboard bindings** — worth a look before we build any
Gate 1 review surface in React. Our Gate 1 is a triage screen; that is exactly
this shape.

### 1.5 Imagen AI / commercial culling — **CLOSED**
Cloud, subscription, sends photographs to a third party. Fails the operating
principle on both privacy and the fourth-touchpoint test.

---

## 2. Lightroom Classic automation

Ours is `plugins/epd-edit-bridge/` — a Lua plugin (`ApplySettings.lua`,
`ExportSettings.lua`, `QueueProcessor.lua`, `JobFile.lua`, `JsonCodec.lua`)
driven by job files on disk.

### 2.1 lightroom-mcp — **REFERENCE (the highest-value read here)**
`Automaat/lightroom-mcp` · MIT · TypeScript + Lua · 54★ · 146 commits · active

An independent solution to exactly the problem EPD Edit Bridge solves. Ships a
`LightroomMCP.lrplugin` that binds **two `LrSocket` servers on localhost —
58763 for requests, 58764 for responses** — exchanging line-delimited JSON
frames with a Node client, reading the catalog through
`catalog:withReadAccessDo`. Plugin generates an auth token stored locally.
macOS + Windows. Classic only, not cloud.

Exposed surface: photo search and selection, metadata/EXIF read+write, collection
management, keywords and ratings, import/export with format control, **develop
preset application and setting manipulation**.

**Three things to take when `lua-impl` next touches the bridge:**
- **The dual-socket transport as an alternative to job files.** Ours polls a
  queue directory. Theirs is a live socket pair. Sockets are lower latency and
  remove the polling loop; job files survive a Lightroom restart and leave an
  audit trail. **We should stay on job files** — event-sourcing and
  restart-survival are worth more to us than latency — but their frame format
  is a good model if we ever want a synchronous call.
- **The auth token pattern.** We have no auth on the bridge at all. Anything
  local can drop a job file into our queue. Low real risk on a single-operator
  machine, but their token-in-a-local-file approach is about ten lines.
- **Their develop-settings coverage**, as a checklist against ours. They claim
  preset application *and* setting manipulation; if their Lua handles keys ours
  drops, that is free coverage.

MIT, so borrowing Lua is clean. **Adding a Node MCP server to the runtime is
not on the table** — new service, new language runtime, and it duplicates a
bridge we already own.

### 2.2 lightroom-auto-develop — **REFERENCE**
`shuangye/lightroom-auto-develop` · `Auto Develop.lua`

Small, single-file, does one thing: auto-applies develop settings to selected
photos. Worth reading precisely *because* it is small — it is the minimum viable
shape of what `ApplySettings.lua` does, and a good diff target if ours has grown.

### 2.3 lightroom-auto-export-plugin — **REFERENCE (unverified)**
`jankuca/lightroom-auto-export-plugin`

Exports the library to a location automatically in the background; user defines
export options as a preset, plugin triggers on picked/unpicked, manual or
automatic. **Its change-detection mechanism is the interesting part and I could
not read it** — the README documents usage only, and the source is in
`autoexport.lrdevplugin/`. If we ever want "export happens without the operator
pressing anything" (which Gate 2's *export = approval* semantics arguably
imply), read that folder first.

### 2.4 Adobe's own SDK material — **REFERENCE**
- `developer.adobe.com/lightroom-classic/` — the official SDK entry point.
- The **Flickr sample plugin** (`FlickrExportServiceProvider.lua`, mirrored in
  `micdah/LrControl/Docs/`) is Adobe's canonical example of the hooks an export
  service must provide. If we ever make ShopSteward an *export destination*
  inside Lightroom — arguably the cleanest possible Gate 2, since "export to
  ShopSteward" replaces "export then land the file" — this is the file to copy.
- Rob Allen's and Sam Rambles' write-ups are the two decent third-party guides.

**The export-service idea is worth a thought.** Today Gate 2 is: operator
exports to the landing folder, we watch it. An `LrExportServiceProvider` would
make ShopSteward a destination in the export dialog — same number of operator
touches, but the settings come with the file instead of being inferred from it.
Not now; note it for M2b/M3.

### 2.5 `applyDevelopSettings` — the landmine
Undocumented, LR6/CC 2015 onward. Reported limitations:
- **Cannot set Auto settings.** `applyDevelopPreset()` can, via a preset that
  itself sets Auto Tone / Auto WB. If M2b ever wants auto-anything, it must go
  through a preset, not through settings.
- **Crop/geometry support is unclear** — see §0 finding 1.
- There is a filed bug that calling `applyDevelopPreset` with a *plugin* preset
  **removes all photo corrections.** If M2b applies presets rather than
  settings, this is the failure mode to test for first: a preset that silently
  wipes the operator's Gate 2 work would be invisible until a customer complains.

⚠ All three are forum-sourced and unverified. They are hypotheses for the spike,
not facts.

---

## 3. Presets and XMP

### 3.1 The `crs` namespace — **REFERENCE (this is the spec)**
`http://ns.adobe.com/camera-raw-settings/1.0/`

The authoritative parameter list, and the thing to bookmark:
- <https://developer.adobe.com/xmp/docs/xmp-namespaces/crs/> — Adobe's own
- <https://github.com/adobe/xmp-docs/blob/master/XMPNamespaces/crs.md> — same, in git
- <https://exiv2.org/tags-xmp-crs.html> — Exiv2's, often easier to skim

Ranges matter and are not uniform: `Exposure` −4.0…4.0, `Contrast` −50…100,
`Brightness` 0…150, hue/saturation −100…100. **Any corrections proposer must
clamp per-key, not globally.** Crop is `CropTop`/`CropLeft`/`CropBottom`/
`CropRight`/`CropAngle`.

### 3.2 Writing XMP from Python — **BORROW (stdlib-first)**
`python-xmp-toolkit` exists but wraps Exempi (a C library) — that is a system
dependency for what is, in the end, **writing a flat set of attributes into an
RDF blob**. LRTimelapse, the commercial reference implementation, does exactly
this: read the XMP Lightroom wrote, modify values, write it back, Lightroom
picks it up.

**If §0 finding 1 kills the bridge path for geometry, the fallback is
`xml.etree.ElementTree` and a template string, not a new dependency.** A CRS
sidecar is a small, fixed-shape document. Adding Exempi to a Windows nights-and-
weekends project to write twenty attributes would be the wrong trade.

⚠ Caveat that decides this: Lightroom only reads a sidecar back for a photo it
already knows about, and settings-vs-sidecar precedence depends on catalog
state. **The spike must test the round-trip, not just the write.**

### 3.3 preset-generator — **CLOSED**
`FUTC-Coding/preset-generator` · GPL-3.0 · 28★ · 26 commits

GPT-4o writing Lightroom presets, explicitly framed by its author as an
experiment. Output format isn't even documented. The *idea* — an LLM emitting
develop parameters — is real and better demonstrated by JarvisArt (§3.4). GPL-3
on top of a proof of concept is a bad trade for a public repo.

### 3.4 JarvisArt — **CLOSED (license), but it is the proof**
`LYL1015/JarvisArt` · NeurIPS 2025 · 855★ · weights on HF (`JarvisArt-1208`)

Multimodal agent that coordinates 200+ Lightroom tools and **outputs
Lightroom-compatible edit parameters (Lua/XMP), not generative pixels.** It
would therefore *pass* our "AI never touches the photograph" rule — it is
architecturally the thing M2b's corrections proposer wants to be.

**Killed by the license, not the design.** Weights and inference code are
non-commercial; this is a business. Datasets (MMArt-PPR10K, 55K samples;
MMArt-Bench) are Apache-2.0 and could inform an eval set if we ever build one.

Keep it as the existence proof that instruction → develop-parameters works at
quality, so we don't have to prove that from scratch.

### 3.5 JarvisEvo — **CLOSED (twice)**
`LYL1015/JarvisEvo` · CVPR 2026 · 424★ · JarvisEvo-8B, ~17GB

Same group. Adds **Qwen-Image-Edit for generative tasks** — object removal,
style transfer — alongside Lightroom's preservative adjustments. Non-commercial
license *and* it violates the hard rule that AI never touches the photograph.
Two independent disqualifications. Do not revisit.

---

## 4. Lightroom cloud (Lightroom CC) — **CLOSED**

The Lightroom Services API (`lr.adobe.io`) is real and does what you'd want:
cloud catalog of assets, metadata, renditions, albums; and via Firefly Services
it exposes auto-tone, auto-straighten and preset application.

**It is gated.** Adobe's own docs: *"These APIs are available only to entitled
partner applications that have authenticated the customer."* Partners register
an integration to obtain a client ID, and calls need the `lr_partner_apis` and
`lr_partner_rendition_apis` scopes on top of `openid`/`AdobeID`. The community
Python wrapper `lou-k/lightroom-cc-api` (GPL-3.0, 14★, author's own words:
*"This project is new and needs a lot of work"*) says you just make an app in
the Adobe console — but it does not confirm the partner scopes are grantable to
an individual, and Adobe's docs say they are not.

**Verdict: closed for v1.** Three reasons, any one sufficient: partner gating we
would have to negotiate; it is the *cloud* product while our operator works in
Classic; and it would put the photographs on Adobe's servers to be processed.
Revisit only if we ever go multi-tenant SaaS, where partner status is plausible
and the cloud catalog stops being a downside.

---

## 5. RAW formats

### canon_cr3 — **REFERENCE (docs only)**
`lclevy/canon_cr3` · GPL-3.0 · 326★ · 144 commits · updated Nov 2025 ·
contributors include the ExifTool author

Reverse-engineered specification of Canon's CR3 container (ISO BMFF-based) and
the CRX codec, plus `parse_cr3.py`. Covers metadata, HDR, Cinema Raw Light, and
dual-pixel.

**We don't decode CR3.** Ingestion pairs RAW+JPEG by base filename and scores the
JPEG; metadata comes from ExifTool. This becomes relevant only for **M2a RAW-only
ingest**, where there is no JPEG to score and we need an embedded preview — the
document tells you where the preview lives in the container.

⚠ **GPL-3.0.** This repo is public but not GPL. Linking or vendoring
`parse_cr3.py` would force our licence. **Read the documentation, don't import
the code.** If M2a needs preview extraction, ExifTool
(`-b -PreviewImage` / `-JpgFromRaw`) or `rawpy`/LibRaw gets there without the
licence question.

---

## 6. Print-on-demand

### 6.1 Gelato — **ours, unchanged**
Sole supplier. Templates → `POST` create-from-template → product appears in the
connected Etsy store. Landmines already in `TODO.md`: template `variants[].id`
≠ store-product variant id; `placeholder` is named after the file used to build
the template, not `"ImageFront"`.

⚠ `dashboard.gelato.com/docs/` returns **403 to unauthenticated fetches** — the
API docs are login-walled, so the agent cannot read them. Anything we need from
Gelato's docs has to come from the operator or from the probe script. Worth
knowing before someone burns a session on it again.

### 6.2 Printify — **REFERENCE (the fallback, if we ever need one)**
Documented and verified: rate limits of **600 req/min global, 100 req/min on
catalog, and 200 req / 30 min on product publishing** — the existence of a
publish-specific limit is itself the evidence that a publish endpoint exists.
Auth is a **Personal Access Token** for a single merchant (which is us) or
OAuth 2.0 for platforms managing many. Etsy is a **pre-built integration
channel**, connected in Printify's UI rather than through our own Etsy OAuth —
same shape as Gelato, and it means the POD adapter interface would not change.

⚠ **Endpoint paths unverified.** `developers.printify.com` is JS-rendered and
`WebFetch` recovered only the rate-limit table; `api.printify.com` returns 401
without a token, as it should. Before anyone acts on this, get the OpenAPI spec.

**Why it stays a fallback and not a bake-off:** Printify is a marketplace of
independent print providers, so quality is *variable* by design — the reason to
prefer it is per-unit cost across many providers, which matters at 500 orders a
month and not at 10. We sell one canvas at a time under the operator's own name.
Gelato is chosen; this is written down so the fallback is a known quantity rather
than a fresh two-hour research session at a bad moment.

### 6.3 Printful — **CLOSED, permanently**
Order-fulfilment API only: 89 endpoints in the official Postman collection,
**zero** product-creation endpoints, verified by enumeration on 2026-08-04.
See M5b design §0a. This is the fourth time it has been looked at. **Do not
research it a fifth.**

---

## 7. Staging-template mockups (M4)

**The field is thin.** No mature, maintained open-source project does realistic
wall-art mockups end to end. What exists is either toy repos (`Mockup-Generator`,
0 stars, no licence; `etsy-mockup-generator`, Pillow paste only, 1 star) or paid
SaaS (Dynamic Mockups, Mockey, PosterMock AI). **We are not reinventing a solved
problem — we are composing primitives.** That is the finding.

### 7.1 The two primitives to lift — **BORROW**

- **`imutils.perspective.four_point_transform`** · MIT · ~4.6k★ · PyImageSearch.
  The canonical `getPerspectiveTransform` + `warpPerspective` helper. It solves
  the *inverse* of our problem (photo→flat, document de-skew), but the maths is
  identical run backwards (flat→quad on a wall). **Take the corner-ordering and
  output-sizing logic verbatim** rather than deriving homography by hand.
  ⚠ Its known failure mode is our known failure mode: **bad corner ordering
  silently produces a mirrored or rotated warp** — no exception, just a wrong
  picture. Assert `tl/tr/br/bl` ordering in a unit test.
- **`psd-tools`** · MIT · ~1.4k★ · 2000+ commits · actively maintained.
  The genuinely valuable find. It reads PSD structure including smart-object
  layers, and exposes **`layer.smart_object.transform_box`** — the four
  transformed corner coordinates of a smart object's placement, read from the
  `PLACED_LAYER` block.

**Why `psd-tools` matters more than it looks.** The entire commercial mockup
world runs on PSD smart-object templates, sold on Creative Market and similar.
Today those require Photoshop. With `transform_box` we can **buy or download a
professional room-scene PSD, read the four corners of its smart object, and do
our own `warpPerspective`** — no Photoshop, no SaaS, no per-render fee. Adobe
did the hard part (photographing and measuring the room); we skip their runtime.
PosterMock AI is that exact pattern, productised behind a paywall.

⚠ `psd-tools` is **read-only for geometry**. It cannot write or replace
smart-object contents, and does not render most layer effects or blend modes.
It is a geometry source, not a rendering engine. Don't plan around rendering
the PSD.

### 7.2 The realism technique — **BORROW (no new dependency)**
The dominant approach in the commercial mockup world, from the apparel side:
**a greyscale displacement map plus a Multiply blend.** Bake a displacement map
from the room photo's own wall texture and lighting, warp the art subtly with it
so it follows the surface, then multiply the original wall's shadow/highlight
layer back over the composited art.

Both halves are already in our stack: **`cv2.remap` for the displacement, a
`numpy` array multiply for the blend.** This is the answer to "can deterministic
Pillow/OpenCV look professional" — yes, and without adding anything. Gallery-wrap
edges and frame rendering are the same class of problem.

### 7.3 `CTDave001/automated_mockups` — **REFERENCE**
MIT · 69★ · Pillow. Batch POD mockups using **colour-keyed placeholder
detection** — the template marks the art region with a flat known colour and the
code finds it. Cruder than reading PSD geometry but dependency-free and
obvious; a reasonable fallback if `psd-tools` disappoints, and a good format for
templates we generate ourselves (PRD §: vision models generate *empty rooms*,
which we would then need to locate the wall region in).

### 7.4 The vendors do this server-side
`Printfulpy` and similar are thin wrappers around **Printful's own server-side
mockup generation** — not compositing code. Worth knowing so nobody mistakes
them for a library. Gelato likewise. Our local mockups are for Etsy listing
imagery and pre-listing preview, not for duplicating what the POD vendor already
does at fulfilment.

---

## 8. Print-file preparation and the viability gate

**No end-to-end open-source POD print-prep project exists.** Searched GitHub
topics `print-on-demand` and `printing`; results are mockup overlays and
unrelated imaging pipelines. Same conclusion as §7: compose primitives.

### 8.1 The viability gate — **numbers, not a library**
This is roughly ten lines of code, and the numbers are the deliverable:

| Source | Number | Note |
|---|---|---|
| **Gelato (official)** | **150–225 PPI inside the BleedBox** | Our fulfiller. Authoritative — this is the literal target. |
| WhiteWall (pro lab) | 300 PPI close-viewed, **200 PPI canvas/acrylic** at ~2m | The most rigorous public source found |
| Printful | 150 DPI floor, 300 for paper wall art | |
| CanvasPop / CanvasDiscount | 150 sufficient for canvas; **acrylic needs 150–200 minimum** | |

**The mechanism is surface finish, not just viewing distance** — canvas texture
masks the 150→300 difference, while acrylic's gloss and depth *reveal* pixel
grain. That corrects the intuition in our own notes: acrylic is our premium SKU
and it is also the **least** forgiving, so the gate must be per-product, not one
global threshold.

**Implement as a formula, not a lookup table:** max useful PPI ≈
**6878 ÷ viewing distance in inches**. Compute the requirement from
`(print_size_inches, product_viewing_distance)` — defensible in code, and it
generalises when a new SKU appears. Suggested floors: 150 canvas/poster,
200 acrylic, 300 as the no-warning ceiling.

### 8.2 `smartcrop.py` — **BORROW**
`hhatto/smartcrop.py` · MIT · ~273★ · Python port of `smartcrop.js`.

Slides a window of the target aspect ratio over the image and scores each
position on edge detection, rule of thirds, and saturation. **Classical CV, zero
ML, nothing generative** — it passes our hard rule cleanly. This is the answer to
fitting a 2:3 photograph into a 4:5 or 3:4 product **without a destructive
centre-crop**, which is a live defect class for us (portrait heroes already bit
us once on orientation).

Note it computes a crop *window*; keep the full frame and store the window.
`keplerlab/katna` (MIT, ~398★) does the same thing plus faces and saliency but
drags in video keyframe machinery — **REFERENCE**, not worth the weight.

### 8.3 Already in the stack — **BORROW, zero new dependencies**
- **Gallery-wrap bleed:** `cv2.copyMakeBorder(BORDER_REFLECT_101)` or
  `np.pad(mode="reflect")`. There is no open-source "canvas bleed generator" —
  every hit is a manual Photoshop tutorial. Take the outer N-inch strip and
  reflect it outward. Done.
- **Colour management:** Pillow's `ImageCms` (wraps LittleCMS2) —
  `profileToProfile()`, `buildProofTransform()`, `Intent.PERCEPTUAL` for
  photographic content.
- **Output sharpening:** `PIL.ImageFilter.UnsharpMask` implements exactly the
  radius/amount/threshold model the print community tunes. **Resize to final
  print pixels first, then sharpen** — order matters, and radius scales with
  resolution (~1.5 radius / 100 amount at 300 PPI, less at 150).
  ⚠ **Whether Gelato re-sharpens on their end is unverified** — no doc found
  either way. Do not assume they cancel ours; the canvas sample order (TODO #2)
  is the only way to find out.

### 8.4 Soft-proofing — **REFERENCE, don't build**
Valuable in general prepress, low leverage for us: **Gelato does its own
RGB→CMYK conversion and publishes no per-SKU ICC profile to proof against.**
Embed sRGB and move on. `ImageCms` is already available if a printer profile
ever materialises.

### 8.5 Real-ESRGAN — **CLOSED (by our rule, not its licence)**
BSD-3-Clause, so the licence is fine. **Disqualified by "AI never touches the
photograph."** Flagged explicitly because a licence-only screen would have
waved it through — the guardrail is doing work that the licence check cannot.

---

## 9. Etsy tooling

### 9.1 Python clients — **mostly CLOSED, and the graveyard is the lesson**
- `anitabyte/etsyv3` · **GPL-3.0** · 77★ — **CLOSED on licence.** Don't even
  copy from it. Also has gaps (no `UpdateShopReceipt`, no shipping profiles).
- `amitray007/etsy-python-sdk` · **MIT** · 11★ · 139 commits, semantic-release
  CI — genuinely maintained, ~28 resource classes. **REFERENCE**: read it as a
  *checklist* of endpoints we haven't touched (return policies, shipping
  profiles, production partners). Not worth swapping our working adapter.
- `mcfunley/etsy-python`, `priestc/python-etsy`, `npike/etsy-python3`,
  `etsyclient` — **all v2-era, all dead.** Etsy shut v2 down in 2022.
  ⚠ They still rank top in search results. A dead client that 401s everything is
  worse than no client. `notbvdr/Etsy-Keywords-Research` (21★) is archived with
  the maintainer's own note: *"This Script No Working With New Api."*

**Our adapter is ahead of everything found.** That is worth knowing before
anyone proposes replacing it.

### 9.2 SEO and keyword research — **CLOSED, and this is the useful part**
**Etsy's API has no search-volume or query-analytics endpoint.** Confirmed via
standing, repeatedly-requested, unfulfilled feature requests on `etsy/open-api`.
Every commercial tool — eRank, Alura, Marmalead, Sale Samurai — is closed source
and none publishes its methodology, because **there is no data feed to licence.**
They scrape Etsy search and autocomplete. `etsy-serp-scraper` and
`etsy-shop-spy` confirm the mechanism openly.

**Verdict: do not build or adopt a scraper.** ToS-grey, operationally fragile,
and a maintenance burden a single operator should not own. Traffic is our binding
constraint, so the temptation here is real — but the higher-leverage move is
off-Etsy (Google Trends, Pinterest Trends, cross-referenced by hand), not a
scraper we have to keep alive.

### 9.3 Shop analytics — **REFERENCE (a pattern, not a library)**
Confirmed across `etsy/open-api` discussions #1304, #1386, #681: the API exposes
only **lifetime cumulative views per listing.** No favourites, no visits, no time
windows, no history. Years-old unresolved request.

**Any tool showing "views this week" is polling and snapshotting client-side.
There is no other mechanism.** `cmd-not-found/etsy-polling-lambda` is the only
prior art and it is just that pattern: cron, poll, persist.

**This independently validates what we already built.** Our event-sourced SQLite
with `proj_listing_daily` preserving history *is* the correct design, and the
note in `TODO.md` that "views/favourites do not come back retroactively, the
series starts the day you first sync" is not a limitation of our implementation
— **it is a hard platform limit nobody has solved.** Stop looking for a better way.

### 9.4 Digital delivery — **a load-bearing constraint, not a TODO**
- **Static** digital files: Etsy delivers automatically once attached at
  listing creation. Nothing to automate. Already how M5a works.
- **Per-order personalised files: there is no API to attach a file to a
  specific order after purchase.** Confirmed via `etsy/open-api` #1301, with
  Etsy contributors stating there is no programmatic path into Etsy Chat.
  Sellers upload by hand or email.

⚠ **Write this into any future design that assumes personalised digital
delivery.** It cannot be automated end to end — not a tooling gap, a platform
wall. No open-source project solves it because it is unsolvable from outside.

### 9.5 Listing renewal — **CLOSED as a research question**
$0.20 on expiry at 4 months, auto-renew is a listing-level flag. Community
consensus converges: **renewal affects recency only, not a full re-index** — so
renew-for-SEO is cargo cult. Track expiry, cron a renew call. ~20 lines against
the API we already have.

### 9.6 Prior art — thin
`devonjhills/etsy-digital-mockup-tools` (MIT, 11★) is the closest: mockups +
LLM listing copy + OAuth2/PKCE upload + bulk creation. Worth a skim for the
mockup structure and PKCE flow. **REFERENCE.**

**No project was found doing Etsy + POD + listing enrichment end to end at any
maturity.** Our "POD creates the listing, we enrich it" pattern appears to have
no open-source prior art at all. Genuinely under-explored, not reinvented.

---

## 10. Social promotion — **the honest answer is "not yet"**

### 10.1 instagrapi and the unofficial path — **CLOSED**
`subzeroid/instagrapi` · MIT · very active. Licence is fine; **the risk is not.**
Its *own README* now steers business users to the official API and calls
private-API automation fragile in production. Reported suspension rates:
**<0.5%/yr for official-API tools vs 15–30%/yr for app-emulating automation.**
We would be betting the shop's only Instagram account to save an app review.

### 10.2 Self-hosted schedulers
- `gitroomhq/postiz-app` · **AGPL-3.0** · 34.3k★ — **CLOSED as a dependency.**
  AGPL is *stricter* than GPL for anything network-served, which is what we are.
  Safe to read its Instagram/Pinterest client code for API-call shape; never vendor.
- `inovector/mixpost` · **MIT** · Laravel — licence fine, but **Instagram is
  gated to the paid tier and is not in the open-source core.** REFERENCE only.

### 10.3 The official Instagram API — clean but mismatched
Content Publishing API, confirmed against Meta's docs: Professional
(Business/Creator) account linked to a Facebook Page, Page Publishing
Authorization, a Meta developer app, and app review for
`instagram_business_content_publish` — **2–4 weeks, one-time.** Two-step publish
(container → `media_publish`). **JPEG only, no PNG.** Carousels ≤10. No product
tags via API, no alt text on Reels/Stories.
⚠ Rate limit sources disagree — 100 posts/24h vs 25. **Unverified; don't design
near either ceiling.**

**The problem is not the API, it's the content.** Converging sources: **Reels
drive Instagram discovery; static feed photos reach only existing followers.**
Our pipeline produces a single composited photograph. An automated feed post is
therefore *"notify the followers we already have"*, not *"acquire traffic"* —
and traffic is the entire constraint. Building it would ship a feature whose own
evidence says it won't move the needle.

### 10.4 Pinterest is the better bet — and it's testable for free
Pinterest API v5 is free at Trial and Standard, OAuth, generous limits (1000
read/min, 100 write/min), and structurally better suited: **evergreen search
traffic rather than an ephemeral feed.**
⚠ **Unverified:** one source says pins created under Trial access are hidden
from the public until a video-demo review passes. If true that is a gate
comparable to Meta's. Verify against Pinterest's own docs before writing code.

**✅ Already done — the shop is claimed on Pinterest** (operator, confirmed
2026-08-05). Rich Pins have therefore been live, meaning every listing already
shows live price and availability on any pin. The claim is *not* an auto-poster;
it enriches pins that already exist.

**So the free experiment has already been running, and it is producing.** A
Pinterest milestone email on 2026-08-05 reports **a single pin at 250
impressions — and the pin is the black bear cub**, the one subject that has sold
at *both* ends of the ladder ($7.99 download and a $107.67 canvas). For scale,
the shop has 1,754 *lifetime* Etsy views. One pin is generating impressions at a
rate the Etsy listing page never has.

⚠ **Impressions are not clicks, and a milestone email is not analytics.**
Pinterest fires these at round numbers; it is engagement bait, and 250
impressions on one pin says nothing about outbound clicks to Etsy. **The
decisive number is still Etsy Stats → Traffic Sources.** But this moves
Pinterest from "untested hypothesis" to "live channel with unmeasured
conversion," which is a materially different question.

If Pinterest is already a meaningful share of those 1,754 views, an adapter is
justified. If it is negligible *despite* pins earning impressions, that is also
a result — it would mean pins get seen but don't convert to visits, and the fix
is pin copy and destination, not more automation.

⚠ Etsy's API exposes no traffic-source data (consistent with §9.3), so this
reading is manual and cannot be pulled into the shop brief.

**Bottom line for the whole lane: don't build it yet** — but for a better reason
than "untested." The cheap test was already run; go read it.

---

## Licence summary

| Project | Licence | Safe to vendor? |
|---|---|---|
| pixcull | MIT | yes (we won't — too big) |
| facet | MIT | yes |
| lightroom-mcp | MIT | yes (Lua only) |
| imutils | MIT | yes |
| psd-tools | MIT | yes |
| smartcrop.py | MIT | yes |
| automated_mockups | MIT | yes |
| etsy-python-sdk | MIT | yes |
| etsy-digital-mockup-tools | MIT | yes |
| mixpost | MIT | yes (but IG is paid-tier) |
| PhotoSort | Apache-2.0 | yes |
| Katna | MIT | yes (too heavy) |
| MMArt datasets | Apache-2.0 | yes |
| Real-ESRGAN | BSD-3 | licence fine — **blocked by our own rule** |
| canon_cr3 | **GPL-3.0** | **no** — docs only |
| preset-generator | **GPL-3.0** | **no** |
| lightroom-cc-api | **GPL-3.0** | **no** |
| etsyv3 | **GPL-3.0** | **no** |
| postiz | **AGPL-3.0** | **no** — worse than GPL for a served app |
| JarvisArt / JarvisEvo | **non-commercial** | **no** — this is a business |

**The rule: ShopSteward is MIT** (`pyproject.toml`, `LICENSE`) and is going
open source on GitHub. MIT is permissive, GPL-3 is copyleft, and they only
travel in one direction — **vendoring or linking GPL-3 code would force the
whole project to GPL-3.** Being open source does not make this go away; it is
precisely the situation where it bites, because a downstream user's rights
change. Three of the most immediately tempting repos here are GPL-3
(canon_cr3, preset-generator, lightroom-cc-api) and two are non-commercial.

Read them; don't import them. MIT and Apache-2.0 (pixcull, facet,
lightroom-mcp, PhotoSort) are safe to borrow from — keep the upstream copyright
notice with anything we copy verbatim.

---

## What this changes

### Do now (no code)
- **Read Etsy Stats → Traffic Sources.** The shop is already claimed on
  Pinterest, so Rich Pins have been live and the free experiment has already
  run — the answer exists today. Whether Pinterest shows up decides the social
  adapter question outright, in either direction. §10.4.

### Carry into M2/M3/M4
1. **TODO #3's spike has a stated hypothesis** (geometry keys dropped) and a
   stated fallback (§3.2, `ElementTree`, no new dependency). Test the sidecar
   *round-trip*, not just the write.
2. **The 6-axis rubric and XMP-sidecar transport** (§1.1) before M3 scoring.
3. **`psd-tools` + `warpPerspective` is the M4 mockup architecture** (§7.1).
   Buy professional room-scene PSDs, read the smart object's four corners, warp
   ourselves. Skips Photoshop and skips the SaaS paywall.
4. **Displacement map + Multiply blend** (§7.2) is how deterministic compositing
   reaches professional quality — `cv2.remap` and a numpy multiply, no new
   dependency. Settles any doubt about the "no generative fill" rule costing us
   realism.
5. **`smartcrop.py` for aspect fitting** (§8.2) — classical CV, passes the hard
   rule, fixes destructive centre-crops.
6. **Acrylic is the least forgiving SKU, not the most** (§8.1) — gloss reveals
   grain that canvas texture hides. The viability gate must be per-product.
   Gelato's own number is **150–225 PPI in the BleedBox**.

### Now settled, stop looking
7. **Etsy has no search-volume API and never has.** Every SEO tool scrapes.
   Don't build one, don't adopt one (§9.2).
8. **Etsy gives lifetime cumulative views only — no history, ever.** Our
   poll-and-snapshot event sourcing is not a workaround, it is the only known
   design (§9.3).
9. **Per-order personalised digital files cannot be delivered via API** (§9.4).
   A platform wall. Any design assuming otherwise is wrong.
10. **Printify is the POD fallback, Printful is not** (§6).
11. **Don't build Instagram automation yet** (§10). Not licence, not access —
    evidence. Our pipeline makes stills; stills don't drive discovery.

### The pattern across all of it
Three times over — mockups (§7), print prep (§8), and Etsy+POD end to end
(§9.6) — the search returned **no mature open-source project at all.** That is
not a gap in the searching. These are real gaps, and it means ShopSteward is
composing primitives rather than reinventing frameworks. Worth remembering when
this repo goes public: the parts with no prior art are the parts worth
publishing.
