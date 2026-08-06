# Vision model cost/quality follow-up

*2026-08-05, first pass. Raised during the Alaska batch's live scoring run,
when the Pro-escalation rate came in high (71% of the first 13 photos).
Actual spend was trivial, so nothing here blocked that run.*

*2026-08-05/06, second pass — corrects the first pass. The first pass used
web search + article summaries and concluded Qwen3-VL doesn't support
OpenRouter strict-mode JSON. That was checked directly against OpenRouter's
live model catalog (`GET /api/v1/models`, `supported_parameters` field) and
is **wrong as of this check** — see below. Querying the catalog directly is
the reliable method; treat prose summaries of "which models support X" as
unverified until cross-checked against the catalog itself, since an
AI-summarized fetch of a large model list has already been caught
inventing plausible-sounding model names once this session.*

## What was asked

Would switching the commercial-scoring vision calls (currently
`google/gemini-2.5-flash-lite` triage + `google/gemini-2.5-pro` escalation,
both via OpenRouter, `config/defaults/tuning_profile.json` `vision` block)
to a cheaper model save meaningful money (and, after the second pass,
time — escalation calls measured at ~13-15s each) without hurting quality.

## Findings, verified against the live OpenRouter model catalog

| Model | In / Out $/Mtok | Vision input? | `structured_outputs` (strict)? |
|---|---|---|---|
| `google/gemini-2.5-flash-lite` (current triage) | $0.10 / $0.40 | Yes | Yes |
| `google/gemini-2.5-pro` (current escalation) | $1.25 / $10.00 | Yes | Yes |
| `qwen/qwen3-vl-8b-instruct` | $0.117 / $0.455 | Yes, vision-native | **Yes** |
| `qwen/qwen3-vl-32b-instruct` | $0.104 / $0.416 | Yes, vision-native | **Yes** |
| `qwen/qwen3-vl-235b-a22b-instruct` (flagship) | $0.210 / $1.900 | Yes, vision-native | **Yes** |
| `moonshotai/kimi-k2.5` | $0.57 / $2.85 | Yes | Yes |
| `moonshotai/kimi-k2.6` | $0.589 / $2.48 | Yes | Yes |
| DeepSeek (all variants, incl. new v4 line) | — | **No — text-only modality on every DeepSeek model in the catalog, checked directly** | n/a |

**DeepSeek stays ruled out** — reconfirmed directly against the catalog,
not just prose: every `deepseek/*` model, including the newest v4 line, has
`input_modalities: ["text"]`. No vision at all via OpenRouter. Closed.

**Qwen3-VL now genuinely supports OpenRouter strict-mode JSON** — the
`structured_outputs` flag is `true` for all three Qwen3-VL instruct
variants checked. This reverses the first pass's blocker finding. Whether
that was ever accurate or changed since is unknown and doesn't matter; the
catalog is the source of truth going forward.

**The real lever is the escalation model, not triage.** Triage
(`gemini-2.5-flash-lite`, $0.10/$0.40) is already near the price floor for
a usable vision model — Qwen3-VL's cheapest variants ($0.104-0.117 in) are
not meaningfully cheaper. But escalation (`gemini-2.5-pro`, $1.25/$10.00)
is where the money AND the time go: it's the ~13-15s/call leg measured in
`docs/research/2026-08-05-vision-scoring-bottleneck.md`, and it fires on
most photos (71% observed escalation rate). `qwen/qwen3-vl-235b-a22b-instruct`
at $0.21/$1.90 — flagship-size, vision-native, strict-mode-capable — is
~6x cheaper on input and ~5x cheaper on output than Gemini Pro for the
one call that actually costs real money and real time. `kimi-k2.5`/`k2.6`
are a secondary, less-vision-specialized option in the same rough price
band if Qwen doesn't hold up on quality.

No aesthetic/commercial-photo-judgment-specific benchmark exists for any of
these models — a 2026 Visual Aesthetic Benchmark paper found even frontier
models score far below human experts on real aesthetic judgment (a Claude
model at ~26.5% vs. ~69% human baseline). This whole task class is
genuinely hard for current models regardless of which one this pipeline
uses, which argues for verifying on real photos before trusting a swap, not
for treating this as settled by benchmarks alone.

**One real remaining caveat, not a blocker:** the catalog's
`structured_outputs` flag reflects capability across a model's available
provider endpoints in aggregate. OpenRouter's automatic routing could still
send a specific request to an underlying provider that only implements the
looser `json_object` mode. This is a live-test question, not a code
question — the fix if it happens is pinning a specific provider in the
request body, not new adapter logic.

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

## How a swap would actually be implemented

No new adapter code. `adapters/vision/openrouter.py` sends the same request
shape (image + `response_format: json_schema, strict: true`) to the same
single endpoint regardless of which model string is passed — OpenRouter
normalizes the interface across providers. The `model` name is a config
value (`config/defaults/tuning_profile.json` → `vision.rescore_model`),
not something with different "commands" per model. A swap is:

1. Add a pricing entry to `vision.est_cost_per_mtok` for the new model id.
2. Change `vision.rescore_model` to the new model id.
3. Run a small `--live-vision` batch (same operator-gated triple-gate as
   any live call) and confirm real responses come back strict-JSON-valid —
   this is the step that actually verifies the one open caveat above
   (provider routing), not a code change.

If that holds up, no further implementation work. If OpenRouter routes to
a provider that doesn't honor strict mode for that model, the fix is a
`provider` field in the request body pinning a specific upstream (still
config/request-shape, not new logic).

## Recommended next step

1. Run `qwen/qwen3-vl-235b-a22b-instruct` side by side against
   `gemini-2.5-pro` as the escalation model on a sample of **already-scored**
   photos (composite scores and Gate 1 outcomes are already recorded — no
   new photography needed). Compare verdicts, not just cost, and capture
   per-call timing the same way the bottleneck investigation did.
2. Only promote it into `config/defaults/tuning_profile.json` if verdicts
   agree closely enough to trust on real curation decisions — this changes
   what does or doesn't become a sellable listing, so treat it with the
   same scrutiny as any other model/provider decision (CLAUDE.md: AI
   model/provider selection needs operator review).

Not blocking anything — current spend is a few dollars/month, well under
the $10 soft cap. This is a real, verified, cost-and-latency lever now
(escalation is ~85% of both cost and per-photo wall-clock time), worth
doing before scaling to the remaining ~900 unpaired R7 photos, but not an
emergency.

## Side-by-side result (2026-08-06) — do not promote yet

Ran `qwen/qwen3-vl-235b-a22b-instruct` against 5 photos that had already
escalated to `gemini-2.5-pro` in production, reusing the real
`OpenRouterVisionAdapter` unchanged (only the model string differs), and
compared against the stored Gemini Pro verdict for the same photos.

| Photo | Gemini Pro | Qwen3-VL | Diff |
|---|---|---|---|
| 07a9a8b3 | 65 | 72 | +7 |
| 041f926a | 35 | 68 | +33 |
| 3b5809e6 | 25 | 58 | +33 |
| 39d41629 | 55 | 78 | +23 |
| 389dadd9 | 45 | 78 | +33 |

**Technically solid**: 5/5 calls returned valid strict-JSON (confirms the
catalog's `structured_outputs: true` flag holds in practice, not just on
paper). **Faster on 4/5 calls** — 3.1s/5.3s/4.5s/7.2s vs the ~13-15s Gemini
Pro baseline; one call ran 15.8s, so latency has real variance, but the
average is a clear win.

**Scoring is not equivalent — Qwen is a systematically more lenient
grader.** Every photo scored higher under Qwen, by +7 to +33 points,
averaging +26. Against `gate1_threshold=60`, three photos Gemini clearly
rejected (35, 25, 45) would have cleared or nearly cleared the bar under
Qwen (68, 58, 78). This is a calibration shift, not noise — swapping the
model as-is would quietly loosen what counts as sellable.

**Verdict: do not promote to `tuning_profile.json` as-is.** The technical
viability question (does it work, is it fast, is it cheap) is answered:
yes. The judgment-equivalence question is not — n=5, all skewed the same
direction, is enough to block a swap but not enough to derive a reliable
correction offset. Next step if this gets picked back up: either a larger
sample to characterize the bias with confidence, or a recalibrated
`gate1_threshold` specifically for Qwen, verified against real Gate 1
outcomes before trusting it on inventory that matters.

## Prompt anchoring attempt (2026-08-06) — improved, not resolved

Research (LLM-as-judge calibration literature — see FutureAGI's rubric
guide and related 2026 sources) confirms the likely root cause: the prompt
gave `commercial_score` as a bare "integer 0-100" with zero anchors, so
each model falls back to its own internal prior for what the scale means.
A cited legal-eval case study went from 0.42 to 0.78 inter-rater agreement
just by adding explicit per-band anchors with observable criteria and an
explicit instruction against generous defaults ("reserve the top band, do
not round up").

Rewrote `config/defaults/prompts/commercial_score.txt` with five explicit
score bands (0-24 / 25-44 / 45-64 / 65-84 / 85-100), concrete criteria per
band, and an explicit "most photos land in 45-64, high scores should be
rare" instruction. Re-ran the same 5 photos against Qwen only (Gemini
verdicts are the same stored production values as the first pass — this
isolates the prompt as the one changed variable for Qwen; it does not
verify the new prompt against Gemini).

| Photo | Gemini Pro (old prompt, stored) | Qwen, old prompt | Qwen, anchored prompt |
|---|---|---|---|
| 07a9a8b3 | 65 | 72 (+7) | 58 (-7) |
| 041f926a | 35 | 68 (+33) | 58 (+23) |
| 3b5809e6 | 25 | 58 (+33) | 58 (+33) |
| 39d41629 | 55 | 78 (+23) | 68 (+13) |
| 389dadd9 | 45 | 78 (+33) | 55 (+10) |

Average bias: **+25.8 → +14.4**. Real improvement, roughly halved, and
latency dropped further (avg ~3.7s vs ~7.2s vs Gemini's ~13.7s baseline).

**New artifact, not previously seen:** three different photos (waterfall,
bear cubs, "wild animals" — visually distinct subjects) all scored exactly
58, the top edge of the "marginal" band. Reads as the model defaulting to
a safe boundary value under uncertainty rather than actually discriminating
between them — a different calibration problem that happens to reduce the
average gap without necessarily fixing genuine judgment quality.

**Still do not promote.** +14 average bias plus identical-score clustering
on 3/5 photos is progress, not a resolved comparison. n=5 twice over is
enough to see a direction, not enough to trust with real curation
decisions or to confidently re-tune `gate1_threshold` against. Also
unverified: whether the anchored prompt shifted Gemini's own calibration
(it wasn't re-run against Gemini in this pass) — since the same prompt
file serves both triage and rescore for whichever provider is configured,
that needs checking before this prompt version could be trusted in
production even for the existing Gemini-only path.

Next step, if picked back up: a larger sample (15-20+ photos, spanning the
score range) run through both models with the anchored prompt, including a
fresh Gemini pass to confirm the prompt change didn't disturb the existing,
already-working calibration.
