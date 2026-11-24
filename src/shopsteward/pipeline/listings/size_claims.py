"""Deterministic rewrite of a live description so it no longer claims print sizes
the file plan cannot deliver (design 2026-11-21-multi-ratio-digital-files
section 8, slice 5a). Pure: text in, text out, no DB/Etsy I/O (the AI-disclosure
wording is read from the same wording file the copy generator uses).

Handled shapes (anything else is left alone and fails the final acceptance check,
so the caller gets None = "hand edit"):
- a line that is only one undelivered size ("✔ 16x20", "A2", "24x16 in"): dropped;
- a run of >=2 sizes joined by `,` `·` `•` or a final "and"/"or": undelivered items
  and their separators are removed, the final and/or moves to the new last item, a
  "N sizes" / "N print sizes" count on the same line (exactly one, equal to the
  run length) is rewritten ("1 size" for one item) and no other integer is touched;
  a run with nothing left drops its line only if that line is just an optional
  bullet, an optional "Sizes:"/"Dimensions:" label, the run and final punctuation;
- any other prose around a run, a count that disagrees, or a count left on an
  unedited line means a hand edit (None);
- the old AI-disclosure sentence is swapped for the one in the wording file.

Line endings and untouched lines are preserved byte for byte.
"""

import re
from collections.abc import Callable

from shopsteward.pipeline.listings.copy import load_wording
from shopsteward.pipeline.listings.digital_files import DigitalFilesConfig, FilePlan
from shopsteward.pipeline.listings.size_guard import _A_SIZE, _PAIR, undelivered_sizes

DeliveredCheck = Callable[[list[str], FilePlan, DigitalFilesConfig], list[str]]

# gap between two list items: `,` `·` `•` (+ optional and/or) or a bare and/or
_SEP = re.compile(r"\s*(?:[,·•]\s*(?:(?:and|or)\s+)?|(?:and|or)\s+)", re.IGNORECASE)
_HAS_AND = re.compile(r"\b(?:and|or)\b", re.IGNORECASE)
_BULLET = re.compile(r"\s*(?:[✔✓☑•·*\-–—]\s*)?")
# a count of sizes ("8 sizes", "3 print sizes"); no other integer is ever touched
_COUNT = re.compile(r"(?<![\w.:])(\d+)(\s+(?:print\s+)?size)(s?)\b", re.IGNORECASE)
# bullet + optional size-word label -- the only prose a dropped list line may hold
# any other way of writing a count of sizes ("eight printable sizes"): never rewritten,
# so wherever one survives an edit the text needs a human
_BROAD = re.compile(
    r"(?<![\w.:])(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"\s+(?:[a-z-]+\s+){0,2}sizes?\b",
    re.IGNORECASE,
)
_LABEL = re.compile(
    _BULLET.pattern + r"(?:(?:(?:available\s+)?(?:print\s+)?sizes?|dimensions)\s*:\s*)?",
    re.IGNORECASE,
)
_TAIL = re.compile(r"\s*[.;:!]?\s*")
_OLD_SENTENCE = re.compile(
    r"The photograph itself is the artist(['’])s original work "
    r"and is never AI-generated or AI-edited\."
)


class _HandEdit(Exception):
    """The line needs a human."""


# no spaces, so "Total of 8: 4x5" is never read as a ratio
_RATIO = re.compile(r"(?<![\w.])\d+:\d+(?!\w|\.\d)")
# a number with a resolution unit ("300 DPI", "24 MP"): a measurement, never a count
_UNIT = re.compile(r"(?<![\w.])\d+\s*(?:dpi|ppi|px|mp|megapixels?)\b", re.IGNORECASE)


def _exempt(line: str) -> list[tuple[int, int]]:
    """Size tokens, aspect ratios ("2:3") and resolutions ("300 DPI"): never counts."""
    items = _items(line)
    ratios = [m.span() for m in _RATIO.finditer(line)]
    ratios = [r for r in ratios if not any(r[0] < b and a < r[1] for a, b in items)]
    return items + ratios + [m.span() for m in _UNIT.finditer(line)]


def _items(line: str) -> list[tuple[int, int]]:
    """Spans of size tokens (the trailing whitespace `_PAIR` swallows is excluded)."""
    spans = [(m.start(), m.end(4) if m.group(4) else m.end(3)) for m in _PAIR.finditer(line)]
    spans += [(m.start(), m.end()) for m in _A_SIZE.finditer(line)]
    return sorted(spans)


def _runs(line: str, spans: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    runs: list[list[tuple[int, int]]] = []
    for span in spans:
        if runs and _SEP.fullmatch(line[runs[-1][-1][1] : span[0]]):
            runs[-1].append(span)
        else:
            runs.append([span])
    return [r for r in runs if len(r) >= 2]


_WORDS = [
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
]


def _stray_counts(text: str, ns: set[int], rewritten: set[tuple[int, int]]) -> bool:
    """A standalone N (digit or number word, "dozen" for 12) for any edited run of
    N items left anywhere outside size tokens and the one rewritten count."""
    for i, line in enumerate(text.split("\n")):
        spans = _exempt(line)
        for n in ns:
            word = _WORDS[n - 1] if n <= len(_WORDS) else None
            alt = f"{n}|{word}" + ("|dozen" if n == 12 else "") if word else str(n)
            for m in re.finditer(rf"(?<![\w.])(?:{alt})(?!\w|\.\d)", line, re.IGNORECASE):
                if (
                    not any(a <= m.start() < b for a, b in spans)
                    and (i, m.start()) not in rewritten
                ):
                    return True
    return False


# any whole number, number word or Roman numeral in the scope of an edit (a count may
# hide as "(4)", "Total of 8", "VIII sizes"); "one"/"I" are left alone ("one per frame")
_SCOPED = re.compile(
    r"(?<![\w.])(?:\d+|(?i:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen)"
    r"|XII|XI|X|IX|VIII|VII|VI|V|IV|III|II)(?!\w|\.\d)"
)
_QUANTIFIER = re.compile(r"\bsizes\b|\b(?:multiple|several|various|range)\b", re.IGNORECASE)


def _blocks(lines: list[str], idxs: list[int]) -> set[tuple[int, int, int]]:
    """(first, last, heading) per edited line: the contiguous non-blank block and
    the nearest non-blank line above it (-1 if none)."""
    out = set()
    for i in idxs:
        a = b = i
        while a > 0 and lines[a - 1].strip():
            a -= 1
        while b + 1 < len(lines) and lines[b + 1].strip():
            b += 1
        h = a - 1
        while h >= 0 and not lines[h].strip():
            h -= 1
        out.add((a, b, h))
    return out


def _next_block(lines: list[str], b: int) -> range:
    """The next non-blank block below line `b` (empty if none)."""
    c = b + 1
    while c < len(lines) and not lines[c].strip():
        c += 1
    d = c
    while d + 1 < len(lines) and lines[d + 1].strip():
        d += 1
    return range(c, d + 1) if c < len(lines) else range(0)


def _scoped_numbers(lines: list[str], scope: set[int], rewritten: set[tuple[int, int]]) -> bool:
    for i in scope:
        spans = _exempt(lines[i])
        for m in _SCOPED.finditer(lines[i]):
            if any(a <= m.start() < b for a, b in spans) or (i, m.start()) in rewritten:
                continue
            if m.group().isdigit() and int(m.group()) < 2:
                continue
            return True
    return False


def _fix_line(
    line: str,
    plan: FilePlan,
    dcfg: DigitalFilesConfig,
    check: DeliveredCheck,
    ns: set[int],
    starts: list[int],
) -> str | None:
    """The rewritten line, the same line, or None to drop it."""
    spans = _items(line)
    bad = {s for s in spans if check([line[s[0] : s[1]]], plan, dcfg)}
    if not bad:
        return line
    if len(spans) == 1:
        s = spans[0]
        if _BULLET.fullmatch(line[: s[0]]) and not line[s[1] :].strip():
            return None
    edits: list[tuple[int, int, str]] = []
    rewritten: set[int] = set()
    changed = [r for r in _runs(line, spans) if any(s in bad for s in r)]
    for run in changed:
        keep = [s for s in run if s not in bad]
        if not keep:
            if _LABEL.fullmatch(line[: run[0][0]]) and _TAIL.fullmatch(line[run[-1][1] :]):
                return None  # nothing left of the list: drop the line
            raise _HandEdit
        ns.add(len(run))
        and_gap = next(
            (
                g
                for g in (line[a[1] : b[0]] for a, b in zip(run, run[1:], strict=False))
                if _HAS_AND.search(g)
            ),
            None,
        )
        out = line[keep[0][0] : keep[0][1]]
        for j in range(1, len(keep)):
            gap = line[run[run.index(keep[j]) - 1][1] : keep[j][0]]
            if j == len(keep) - 1 and and_gap is not None:
                gap = and_gap if len(keep) > 2 else re.sub(r"^\s*,", "", and_gap)
            out += gap + line[keep[j][0] : keep[j][1]]
        edits.append((run[0][0], run[-1][1], out))
        counts = [m for m in _COUNT.finditer(line) if not any(a <= m.start() < b for a, b in spans)]
        if counts and (len(counts) > 1 or int(counts[0].group(1)) != len(run) or len(changed) > 1):
            raise _HandEdit
        for m in counts:
            noun = m.group(2) + ("" if len(keep) == 1 else m.group(3))
            edits.append((m.start(), m.end(), f"{len(keep)}{noun}"))
            rewritten.add(m.start())
            starts.append(m.start())
    if any(m.start() not in rewritten for m in _BROAD.finditer(line)):
        raise _HandEdit
    for a, b, rep in sorted(edits, reverse=True):
        line = line[:a] + rep + line[b:]
    return line


def _new_sentence(old_apostrophe: str) -> str:
    line = load_wording()["ai_disclosure_line"]
    m = re.search(r"The photograph itself.*", line)
    sentence = m.group() if m else line
    return sentence.replace("'", old_apostrophe)


def fix_description(
    text: str,
    plan: FilePlan,
    dcfg: DigitalFilesConfig,
    delivered_check: DeliveredCheck = undelivered_sizes,
) -> str | None:
    """`text` with undelivered size claims removed and the disclosure sentence
    swapped, or None when the result still claims an undelivered size or nothing
    changed (a human must edit it)."""
    out: list[str] = []
    edited = stale_count = False
    ns: set[int] = set()  # original lengths of the runs that were edited
    rewritten: set[tuple[int, int]] = set()  # (line index, start) of rewritten counts
    dropped: list[bool] = []  # per original line
    results: list[str | None] = []  # per original line: rewritten text, None = dropped
    kept: list[int] = []  # original index of each output line
    for raw in text.split("\n"):
        line, cr = (raw[:-1], "\r") if raw.endswith("\r") else (raw, "")
        try:
            starts: list[int] = []
            fixed = _fix_line(line, plan, dcfg, delivered_check, ns, starts)
            rewritten.update((len(dropped), st) for st in starts)
        except _HandEdit:
            return None
        edited |= fixed != line
        stale_count |= fixed == line and _BROAD.search(line) is not None
        dropped.append(fixed is None)
        results.append(fixed)
        if fixed is not None:
            out.append(fixed + cr)
            kept.append(len(dropped) - 1)
    if edited and stale_count:
        return None  # a size count on a line this rewrite did not touch is now wrong
    lines = text.split("\n")
    for i in kept:  # a "...:" intro whose list was all dropped
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if lines[i].rstrip().endswith(":") and j < len(dropped) and dropped[j]:
            return None
    if _stray_counts(text, ns, rewritten):
        return None
    blocks = _blocks(lines, [i for i, r in enumerate(results) if r != lines[i].rstrip("\r")])
    scope = {
        j for a, b, h in blocks for j in (*range(a, b + 1), h, *_next_block(lines, b)) if j >= 0
    }
    if _scoped_numbers(lines, scope, rewritten):
        return None
    for a, b, h in blocks:
        new = [r for r in results[a : b + 1] if r is not None]
        if sum(len(_items(r)) for r in new) == 1:
            head = [lines[h]] if h >= 0 else []
            if _QUANTIFIER.search(" ".join(new + head)):
                return None  # "several sizes" left with one size
    new = "\n".join(out)
    had_old = _OLD_SENTENCE.search(text)
    new = _OLD_SENTENCE.sub(lambda m: _new_sentence(m.group(1)), new)
    if had_old and _new_sentence(had_old.group(1)) not in new:
        return None  # the disclosure line itself was swept away: never ship that
    if not new.strip() or new == text or delivered_check([new], plan, dcfg):
        return None
    return new
