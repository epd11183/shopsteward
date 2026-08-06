# Vision-scoring bottleneck investigation

*2026-08-05. Triggered by the Alaska batch's live scoring run averaging
~49s/photo (43 photos in 34.9 minutes, measured from `photo.scored` event
timestamps). This document is the record of what was found — read this
before re-investigating scoring performance.*

## What was asked

Why is `shopsteward score run --live-vision` slow, and did we have any
logging in place to see where the time goes.

## Finding 1: no timing instrumentation existed

Before tonight, `src/shopsteward/pipeline/scoring.py` had exactly two
`logger.warning()` calls (the monthly soft-cap check) and nothing else in
the whole scoring path logged anything. Worse: **no file in the codebase
called `logging.basicConfig`**, so even those two warnings only reached the
terminal via Python's bare last-resort handler — no timestamps, no other
level ever visible. There was no way to reconstruct the original slow run's
timing from logs, because none were written.

**Fixed**: `src/shopsteward/cli.py` `main()` now calls
`logging.basicConfig(level=logging.INFO, ...)` once, at the real
console-script entrypoint only (not in the importable `app`, so tests are
unaffected). `src/shopsteward/pipeline/scoring.py`'s per-photo loop now logs
one INFO line per photo: total elapsed, triage-scorer elapsed, rescore
elapsed, whether it escalated, and the final composite score.

## Finding 2: the escalation (Pro) call is the real cost, and it's roughly what you'd expect

Ran a real 5-photo `--live-vision` batch (operator-approved) with the new
logging live. Two photos scored successfully, both escalated to
`google/gemini-2.5-pro`:

| Photo | Total | Triage scorers | Pro rescore |
|---|---|---|---|
| 1 | 15.72s | 2.87s | 12.84s |
| 2 | 17.66s | 3.16s | 14.50s |

Triage (Flash-Lite + technical scorer) is consistently ~3s. The Pro rescore
call is the expensive part at ~13-15s — consistent with the escalation rate
observed earlier this session (71% of an early sample), which means most
photos pay this cost. This alone gets you into the teens-of-seconds range
per photo, not 49s — see Finding 3 for the rest of the gap.

## Finding 3: a real, previously-undiagnosed bug — every verbose Pro rationale was silently discarded, cost and all

**3 of the 5 candidates in the same test batch failed**, and all three
failures were `VisionParseError` on the Pro escalation call specifically —
zero triage-model failures. Root cause is a schema mismatch between two
places that describe the same field and had drifted apart:

- `adapters/vision/openrouter.py` `_VERDICT_SCHEMA` (sent to OpenRouter with
  `strict: True`) describes `rationale` as an unconstrained string.
- `adapters/vision/interface.py` `VisionVerdict.rationale` is
  `Field(max_length=140)`, enforced locally by Pydantic *after* the paid API
  call already succeeded.

OpenRouter/Gemini only enforces the schema it's given — so Gemini 2.5 Pro
(more verbose than Flash-Lite triage) legitimately writes a normal-length
rationale, satisfies the schema it was sent, and gets thrown away locally by
a tighter constraint it was never told about. Confirmed from the real
failures: `finish_reason: 'stop'` (not truncated by the model), valid JSON
up to where the debug logger's own 500-char truncation cuts it off.

**Impact**: every Pro escalation that writes a normal-length rationale was
being paid for and then discarded as a scoring failure — no retry, no
fallback, straight to `photo.score_failed`. This is a real, silent source of
both wasted spend and wasted time (a full ~13-15s Pro round-trip for zero
usable result), on top of Finding 2's baseline cost. It plausibly accounts
for a meaningful share of the gap between the ~15-18s/photo measured here
and the ~49s/photo average from the original overnight-scale run — not
provably all of it (n=2 successes is a small sample), but it is the one
concrete defect found, not a hypothesis.

**Status**: fixed and reviewed. Operator chose to raise the local Pydantic
cap (`VisionVerdict.rationale`, `adapters/vision/interface.py:20`) from 140
to 500 chars, matching reality, rather than tighten the OpenRouter-side
schema or the prompt. `python-impl` made the one-line change plus a
regression test (`tests/adapters/vision/test_openrouter.py`); `reviewer`
approved — no downstream code depended on the old 140-char cap (UI, DB
schema, fixtures all unconstrained/TEXT). Stale `≤140 chars` reference in
`docs/designs/2026-07-03-m3-scoring-gate1-landing.md` corrected to `≤500`.
Not yet committed — pending operator sign-off.

## What this does NOT yet explain

Two successful escalations is not enough to fully account for a 49s
*average* across 43 photos in the original run. Once the rationale-length
fix lands, worth re-running a larger `--live-vision` batch with the new
per-photo logging live and computing the real distribution (not just two
samples) — including whether `monthly_spend()`'s full re-scan of accumulating
`llm.call` events (grows every photo, `pipeline/llm_ledger.py`) becomes
non-trivial at higher event counts. Not measured yet; flagged as the next
thing to check if the fixed run is still notably above ~15-18s/photo.

## Next steps

1. Land and verify the rationale-length fix (in review).
2. Re-run a larger live batch with logging on; compute real mean/distribution,
   not two samples.
3. Only after that: revisit model choice
   (`docs/research/2026-08-05-vision-model-cost-eval.md`) — a model swap
   was queued behind this investigation per the operator's explicit
   sequencing ("investigate the bottleneck, and then investigate optimal
   models").
