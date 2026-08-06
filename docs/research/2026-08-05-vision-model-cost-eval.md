# Vision model cost/quality follow-up — queued, not urgent

*2026-08-05. Raised during the Alaska batch's live scoring run, when the
Pro-escalation rate came in high (71% of the first 13 photos). Actual spend
was trivial (~$0.09 for 7 photos, ~$4-5 projected for the full 380-photo
batch), so nothing here blocked that run. This document exists so the
research isn't repeated and the wrong turn isn't retaken.*

## What was asked

Would switching the commercial-scoring vision calls (currently
`google/gemini-2.5-flash-lite` triage + `google/gemini-2.5-pro` escalation,
both via OpenRouter, `config/defaults/tuning_profile.json` `vision` block)
to a cheaper model — Kimi, DeepSeek, or Qwen were named — save meaningful
money without hurting quality.

## Findings (OpenRouter pricing, current as of this research pass)

| Model | In / Out $/Mtok | Vision via API? | `json_schema strict`? | Vision benchmark vs Gemini |
|---|---|---|---|---|
| `google/gemini-2.5-flash-lite` (current triage) | $0.10 / $0.40 | Yes | Yes | baseline |
| `google/gemini-2.5-pro` (current escalation) | $1.25 / $10.00 | Yes | Yes | baseline |
| Kimi (latest) | $2.90 / $14.00 | Yes | **No** — loose mode only | no published aesthetic/photo benchmarks |
| DeepSeek (latest) | $0.10 / $0.20 | **No — chat UI only, not via API** | No | ruled out regardless of price |
| Qwen3-VL-8B | $0.117 / $0.455 | Yes, vision-native | **No** — loose mode only | beats Gemini 2.5 Pro on MMBench/RealWorldQA; no MMMU or aesthetic-specific numbers found |
| Qwen3-VL-235B (flagship) | $0.20 / $0.88 | Yes, vision-native | **No** — loose mode only | same, larger model |

**Kimi is not a cost lever** — it's more expensive than both current Gemini
models. Was proposed on the assumption it had "caught up" on performance;
whatever its text/coding benchmarks show, it isn't cheaper for this use case.

**DeepSeek is ruled out entirely, independent of price or benchmarks** — its
vision capability shipped to Moonshot's/DeepSeek's chat app only, not the
API/OpenRouter. A model that cannot see the image is disqualified before any
other comparison matters.

**Qwen3-VL is the one real candidate.** Vision-native architecture (not a
text model with vision bolted on), ~10x cheaper than the current triage
model, and beating Gemini 2.5 Pro on some general vision benchmarks
(MMBench, RealWorldQA). No aesthetic/commercial-photo-judgment-specific
benchmark was found for it, and none was found for Gemini either — a 2026
Visual Aesthetic Benchmark paper found even frontier models score far below
human experts on real aesthetic judgment (a Claude model at ~26.5% vs. ~69%
human baseline), which is a sobering data point regardless of which vision
model this pipeline uses: this whole task class is genuinely hard for
current models, which argues for caution on an unproven swap, not urgency.

**The hard blocker for Qwen:** neither Qwen3-VL variant supports
OpenRouter's `response_format: json_schema, strict: true` — only the looser
`json_object` mode. `adapters/vision/openrouter.py` currently depends on
strict mode to guarantee a parseable, schema-conformant response every call
(`_VERDICT_SCHEMA`, `strict: True` at `openrouter.py:88-95`). Swapping to
Qwen as-is would mean either accepting a higher parse-failure rate than
today (`VisionParseError`, `photo.score_failed`) or building client-side
JSON validation + retry logic first — real implementation work, not a model
string change.

## A note on the wrong category, for the record

A pasted "OpenRouter image models" list during this same conversation
turned out to be **image-generation** models (Recraft, FLUX, Nano Banana /
Gemini image models, GPT-5 Image, Seedream) — text/image-to-image
*creation* and *editing* tools, not vision-*understanding* models that take
a photo and return a judgment. Different OpenRouter category entirely.
Several of them (Nano Banana especially, Gemini's image-editing model) are
exactly the class of tool CLAUDE.md's "AI never touches the photograph...
no generative edit, upscale, or fill on a photograph, ever" rule forbids
using on a shop photo. Worth remembering the distinction the next time
someone reaches for an OpenRouter model list for this pipeline: filter for
vision **input** capability, not image output.

## Recommended next step, when someone picks this up

1. Build a small client-side JSON-schema validator + one-retry-on-failure
   wrapper (generic enough to sit in front of any non-strict-mode model, not
   Qwen-specific).
2. Run Qwen3-VL-8B side by side against the existing Gemini triage/rescore
   pipeline on a sample of **already-scored** photos (the composite scores
   and Gate 1 outcomes are already recorded — no new photography needed).
   Compare verdicts, not just cost.
3. Only promote it into `config/defaults/tuning_profile.json` if verdicts
   agree closely enough to trust on real curation decisions — this changes
   what does or doesn't become a sellable listing, so treat it with the
   same scrutiny as any other model/provider decision (CLAUDE.md: AI
   model/provider selection needs operator review).

**Not urgent.** The current pipeline works, and actual monthly spend at
observed escalation rates is a few dollars, well under the $10/mo soft cap.
This is a "worth doing eventually for cost efficiency at scale," not a
"blocking anything" item.
