"""Etsy tag validation shared by every write path that authors tag content
before it reaches `adapters/etsy/live.py::_encode_form_data`, which
comma-joins list fields into Etsy's form-urlencoded write API. A tag
containing a literal comma would silently split into extra tags once
comma-joined on the wire -- undetectable server-side -- so every call site
that can put a tag in front of that encoder must reject one first:
`CopyVerdict` (this package's `interface.py`, LLM-authored listing-creation
copy), `GateEditFields` (`pipeline.listings.models`, operator-authored Gate 3
edits), and `listing.seo_edit` (`pipeline.ops.capabilities.seo_edit`,
LLM-authored SEO edits).

`MAX_TAGS`/`MAX_TAG_LEN` are Etsy's real field limits, not tuning knobs --
the same numbers were duplicated at all three call sites before this module
existed.
"""

MAX_TAGS = 13
MAX_TAG_LEN = 20


def validate_tag(tag: str, *, max_len: int = MAX_TAG_LEN) -> None:
    """Raise ValueError if `tag` is blank, exceeds `max_len`, or contains a
    comma. Callers decide what a ValueError means for them -- pydantic
    field_validators let it propagate into a ValidationError (itself a
    ValueError subclass); `listing.seo_edit` catches it to drop the whole
    field instead (structural validation there is drop-never-clamp)."""
    if not tag.strip():
        raise ValueError("empty tag not allowed (Etsy rejects blank tags)")
    if len(tag) > max_len:
        raise ValueError(f"tag {tag!r} exceeds {max_len} chars")
    if "," in tag:
        raise ValueError(f"tag {tag!r} contains a comma, not allowed")
