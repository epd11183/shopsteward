"""Pure guard: print sizes named in listing text vs. what a file plan delivers
(design 2026-11-21-multi-ratio-digital-files §7). No DB/event I/O.

Rules (data-driven from the DigitalFilesConfig ratios/sizes):
- A "pair" is `<n> [unit] (x | × | by) <n> [unit]`, decimals and up to 3 digits,
  with digit lookarounds so "1920x1080" / "8.5x11" are never half-matched.
- Metric (cm/mm) pairs are converted to mm and accepted only if they equal a
  DELIVERED size that is defined in mm (an A-size, +-1 mm); otherwise flagged.
- Inch pairs (bare, or with ", ″, ', in, inch(es)) are accepted only if they
  equal a DELIVERED size in inches (either order); otherwise flagged.
- Bare pairs (no unit/mark) are NOT print-size claims when either number is
  > 99 (pixel dimensions) or when the pair is a square or a configured ratio's
  short:long that is not itself a configured size label ("2x3", "4x4"). Bare
  pairs where both numbers are <= 5 and neither a square nor such a ratio
  (e.g. "3x5") ARE flagged. Hence "4K", "R5", "5x zoom" never match at all.
- `A0`..`A6` tokens are flagged unless that A-size is delivered ("A1 road"
  is a known, accepted false positive: rare in wildlife copy, fails closed).
"""

import re

from shopsteward.pipeline.listings.digital_files import DigitalFilesConfig, FilePlan, plan_sizes

_UNIT = r"(?:[\"″']|(?:in(?:ch(?:es)?)?|cm|mm)(?![A-Za-z]))"
_PAIR = re.compile(
    rf"(?<!\d)(?<!\d\.)(\d{{1,3}}(?:\.\d+)?)\s*({_UNIT})?\s*(?:[x×]|\bby\b)\s*"
    rf"(\d{{1,3}}(?:\.\d+)?)(?!\d|\.\d)\s*({_UNIT})?",
    re.IGNORECASE,
)
_A_SIZE = re.compile(r"(?<![A-Za-z])A[0-6](?!\d)", re.IGNORECASE)
_MM_TOL = 1.0


def _fmt(n: float) -> str:
    return f"{n:g}"


def _flagged_pairs(text: str, cfg: DigitalFilesConfig, plan: FilePlan) -> list[str]:
    specs = {s.label: s for r in cfg.ratios for s in r.sizes}
    delivered = [specs[label] for label in plan_sizes(plan) if label in specs]
    inch = {tuple(sorted(s.inches)) for s in delivered if s.inches}
    mm = [tuple(sorted(s.mm)) for s in delivered if s.mm]
    all_labels_inch = {tuple(sorted(s.inches)) for s in specs.values() if s.inches}
    ratios = {tuple(sorted((r.short, r.long))) for r in cfg.ratios}
    out: list[str] = []
    for m in _PAIR.finditer(text):
        a, b = float(m.group(1)), float(m.group(3))
        unit = (m.group(2) or m.group(4) or "").lower()
        pair = tuple(sorted((a, b)))
        if unit in ("cm", "mm"):
            k = 10.0 if unit == "cm" else 1.0
            ok = any(
                abs(pair[0] * k - s[0]) <= _MM_TOL and abs(pair[1] * k - s[1]) <= _MM_TOL
                for s in mm
            )
            if not ok:
                out.append(f"{_fmt(a)}x{_fmt(b)}{unit}")
            continue
        if not unit:
            if max(pair) > 99:
                continue  # pixel dimensions
            if pair not in all_labels_inch and (a == b or pair in ratios):
                continue  # "2x3" aspect ratio, "4x4" square
        if pair not in inch:
            out.append(f"{_fmt(pair[0])}x{_fmt(pair[1])}")
    return out


def undelivered_sizes(texts: list[str], plan: FilePlan, cfg: DigitalFilesConfig) -> list[str]:
    """Sorted, de-duplicated size tokens in `texts` that `plan` does not deliver."""
    delivered_a = {s.upper() for s in plan_sizes(plan)}
    found: set[str] = set()
    for text in texts:
        found.update(_flagged_pairs(text, cfg, plan))
        found.update(tok.upper() for tok in _A_SIZE.findall(text) if tok.upper() not in delivered_a)
    return sorted(found)
