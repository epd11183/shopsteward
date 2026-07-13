# Generating staging room templates (operator guide)

Prompts for producing the ~15 AI-generated empty-room scenes the mockup
compositor stages prints into (PRD §13 decisions 7 and 28: AI-generated
rooms only, produced offline with consumer Gemini/ChatGPT, no runtime
scene-gen adapter). Drop the images into `data/staging_templates/`, then
annotate each in the Templates tab.

## What the compositor needs from every image

These map directly to the sidecar validator in
`src/shopsteward/mockups/templates.py` — an image that violates them will
scan as `stagingtemplate.invalid`:

- **One clearly empty wall span** (two or more for gallery-wall templates)
  with nothing on it — no art, mirrors, windows, TVs, shelves, or sconces
  inside the span you'll annotate. The compositor never erases anything;
  it only composites on top.
- **The wall region roughly faces the camera.** Mild perspective is fine
  (the compositor warps into the quad), but a wall raking away at a steep
  angle makes a bad mockup and risks the concave/edge-length checks.
- **Reference furniture for scale.** You must enter the region's real-world
  width in inches when annotating; a sofa (~84"), queen bed (~60"), or
  console (~48–60") in frame makes that estimate honest.
- **Resolution ≥ 2000 px on the long edge.** The validator requires the
  annotated region's implied resolution to land between 10 and 300 PPI —
  e.g. a 36-inch wall span must be 360–10,800 px wide in the image. At
  2000+ px overall you'll never hit the floor. Ask the tool for its
  highest-quality/largest output.
- **Even, soft lighting on the display wall.** Strong light streaks, hard
  shadows, or blown highlights across the span fight the light-matching
  step and look wrong once a print is composited in.
- **No people, pets, text, logos, or recognizable artwork** anywhere in
  frame (Etsy listing hygiene + the shipped AI-disclosure line only covers
  the room being synthetic).

## Base prompt (single-print templates)

Paste into Gemini or ChatGPT image generation, swapping the bracketed
variation each time:

> Photorealistic interior photograph of a [ROOM + STYLE from the table
> below], shot straight-on at eye level with a 35mm lens. The main wall
> faces the camera and has one large completely empty area — no artwork,
> mirrors, windows, shelves, or decor on that wall. Include [SCALE ANCHOR]
> against or near the wall for scale. Soft, even, diffused natural
> lighting with no harsh shadows or bright streaks on the empty wall.
> Neutral, realistic color grading, sharp focus throughout, high
> resolution, 3:2 landscape aspect ratio.

### Variation table — pick ~12 combinations

| # | Room + style | Wall color | Scale anchor |
|---|---|---|---|
| 1 | modern minimalist living room | warm white | low gray sofa (~84") |
| 2 | Scandinavian living room, light oak floor | pale sage | light linen sofa |
| 3 | cozy rustic living room, wood beams | cream plaster | leather sofa |
| 4 | contemporary bedroom | soft greige | queen bed (~60"), area to hang above headboard |
| 5 | moody modern bedroom | charcoal / deep navy | queen bed, brass lamps |
| 6 | home office / study | muted olive | walnut desk (~60") |
| 7 | mid-century entryway / hallway | white | slim console table (~48") |
| 8 | modern dining room | terracotta accent wall | 6-seat dining table |
| 9 | industrial loft living space | exposed light brick* | dark sofa |
| 10 | coastal living room, airy | off-white shiplap* | slipcovered sofa |
| 11 | reading nook | dusty blue | armchair + floor lamp |
| 12 | minimalist bedroom, portrait-friendly | warm taupe | dresser (~36") with tall empty wall above |

\* Keep the texture subtle — heavy pattern under a composited print reads
as fake. If the result is busy, regenerate with "smooth painted wall".

Rows 4, 5, and 12 tend to produce **portrait/square-friendly** wall spans
(above a headboard or dresser); the rest skew landscape. You want both —
the selector matches template orientation to the photo.

## Gallery-wall prompt (make 2–3 of these)

Gallery-wall templates are one image where you annotate **two to four**
separate quads (the largest gets the hero photo; the compositor fills the
rest with deterministic crops or companions):

> Photorealistic interior photograph of a spacious [modern living room /
> stairway landing / wide hallway], shot straight-on. One very wide,
> completely empty wall dominates the frame — enough blank space to hang
> a cluster of three or four frames, but currently bare. A [long low
> sideboard (~72") / sofa / staircase railing] sits below for scale. Soft
> even daylight, no shadows or streaks on the wall, sharp focus, high
> resolution, 3:2 landscape.

## Per-image acceptance checklist (before annotating)

1. Empty span is genuinely empty and big — at least ~1/6 of the image
   width per intended quad (validator floor is 1.5% of image *area* per
   region, edges ≥ 40 px; eyeball generously).
2. Span is a flat convex rectangle-ish area — annotate corners TL → TR →
   BR → BL; a twisted or concave quad is rejected.
3. Lighting on the span is even; no fixture halos or window glare.
4. You can defend the width-in-inches number from the furniture in frame
   (allowed range 6–120 in).
5. No people, text, watermarks, or identifiable art anywhere.
6. Saved as JPEG or PNG, ≥ 2000 px long edge, into
   `data/staging_templates/`.

Then: `uv run shopsteward serve` → Templates tab → annotate. Rerun the
scan; anything listed invalid tells you which rule it tripped. Committing
the finished set (images + `.template.json` sidecars) is a content-only
PR from `config/defaults/staging_templates/` if we want them shipped as
defaults, or they can stay operator-local in `data/` — your call at PR
time.
