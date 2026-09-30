"""Is a stored quote genuinely verbatim from its source document?

`quote_verified` is the ✓ the dashboard shows beside a royalty, and it is the reason an analyst can
trust a row without opening the source. It therefore has to mean what it says.

Every writer used to compute it as:

    norm(quote)[:80] in norm(text)          # norm = collapse whitespace, strip, lowercase

which checks only the FIRST 80 CHARACTERS. A quote whose opening is verbatim and whose remainder is
paraphrased, truncated, or stitched together from separate sentences passed, was stored in full, and
was badged verified. The truncation was a hedge: with whitespace-and-case normalisation alone, a long
quote is likely to hit a curly apostrophe or an en-dash and fail on a character the reader would
never notice, so cutting it short made verification succeed more often.

The fix is to normalise properly and then check the WHOLE quote. Typography that differs only in
appearance is folded away — curly quotes, the various dashes, non-breaking spaces, HTML entities,
ligatures — so what remains is a real comparison of the words.

Shared by scripts/royalty_pilot.py, ingest_edgar.py, ingest_marketwatch.py and add_one_to_lode.py,
which each had their own copy of the old rule.
"""
from __future__ import annotations

import html
import re
import unicodedata

# Characters that differ only in appearance between a document and a model's transcription of it.
_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",      # single curly quotes
    "“": '"', "”": '"', "„": '"', "‟": '"',      # double curly quotes
    "–": "-", "—": "-", "‒": "-", "―": "-",      # en/em/figure/horizontal dash
    "−": "-", "‐": "-", "‑": "-",                      # minus, hyphen variants
    " ": " ", " ": " ", " ": " ", " ": " ",      # non-breaking / thin spaces
    "​": "", "‌": "", "‍": "", "﻿": "",          # zero-width junk
    "…": "...",                                                  # ellipsis
    "­": "",                                                     # soft hyphen
}
_TAGS = re.compile(r"</?[a-z][^>]*>", re.I)     # the extractor sometimes returns <b>…</b> around a rate
_WS = re.compile(r"\s+")


def normalize(s: str | None) -> str:
    """Fold a string to its comparable form: entities decoded, typography folded, tags and extra
    whitespace removed, casefolded. Two strings equal after this differ only in presentation."""
    if not s:
        return ""
    s = html.unescape(s)
    s = _TAGS.sub(" ", s)
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans(_FOLD))
    return _WS.sub(" ", s).strip().casefold()


def quote_in_normalized(quote: str | None, normalized_source: str) -> bool:
    """As `quote_is_verbatim`, but against text already put through `normalize()`.

    Use this in a loop over one document's royalties so the document is normalised once.
    """
    q = normalize(quote)
    return bool(q) and q in normalized_source


def quote_is_verbatim(quote: str | None, source_text: str | None) -> bool:
    """True only if the ENTIRE quote appears in the source document.

    This is what `quote_verified` stores. Under-claiming is the safe direction: a false negative
    costs an analyst one click to open the source, while a false positive is the badge lying.
    """
    return quote_in_normalized(quote, normalize(source_text))


def verbatim_prefix_ratio(quote: str | None, source_text: str | None) -> float:
    """How much of the quote (0.0–1.0) is verbatim, by longest matching prefix.

    Not stored — this exists to diagnose failures. A ratio near 1.0 means the quote is genuine and
    something small is defeating the comparison; a low ratio means the model really did stitch the
    quote together.
    """
    q, t = normalize(quote), normalize(source_text)
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    lo, hi = 0, len(q)
    while lo < hi:                       # binary search the longest prefix still present
        mid = (lo + hi + 1) // 2
        if q[:mid] in t:
            lo = mid
        else:
            hi = mid - 1
    return lo / len(q)
