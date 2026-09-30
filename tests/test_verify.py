"""Tests for quote verification — the ✓ the dashboard shows beside a royalty.

Pure functions, no database and no Claude calls, so these always run.

    python -m pytest tests/test_verify.py -v
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from techreport.verify import (  # noqa: E402
    normalize,
    quote_in_normalized,
    quote_is_verbatim,
    verbatim_prefix_ratio,
)

DOC = (
    "The Property is subject to a 2.0% net smelter return royalty held by "
    "Franco-Nevada Corporation, of which 1.0% may be repurchased for US$2,000,000 prior to the "
    "commencement of commercial production. A separate 1.0% gross overriding royalty applies to "
    "the northern claim block only."
)


# ---------------------------------------------------------------- the bug this exists to prevent
def test_a_quote_that_is_verbatim_only_at_the_start_is_not_verified():
    """THE regression. The old rule compared `normalize(quote)[:80]` — only the first 80 characters —
    so a quote whose opening was verbatim and whose remainder was paraphrased or stitched from
    elsewhere was stored in full and badged source-verified. Measured against the real EDGAR corpus,
    31 of 408 quotes were passing that way, none of them close to genuine."""
    stitched = (
        "The Property is subject to a 2.0% net smelter return royalty held by Franco-Nevada "
        "Corporation, and the vendors additionally retained a 3.0% gross revenue royalty over the "
        "entire licence area."
    )
    assert normalize(stitched)[:80] in normalize(DOC)   # the old rule would have passed it
    assert quote_is_verbatim(stitched, DOC) is False    # the new rule does not


def test_a_fully_verbatim_quote_is_verified():
    assert quote_is_verbatim(
        "a 2.0% net smelter return royalty held by Franco-Nevada Corporation", DOC) is True


# ---------------------------------------------------------------- typography must not defeat it
@pytest.mark.parametrize("quote", [
    "a 2.0% net smelter return royalty held by Franco‑Nevada Corporation",   # non-breaking hyphen
    "a 2.0% net smelter return royalty held by Franco–Nevada Corporation",   # en dash
    "a 2.0% net smelter return royalty held by Franco-Nevada Corporation",   # non-breaking space
    "a 2.0% net smelter return royalty held by  Franco-Nevada   Corporation",     # runs of whitespace
    "A 2.0% NET SMELTER RETURN ROYALTY HELD BY FRANCO-NEVADA CORPORATION",        # case
    "a 2.0% net smelter return royalty held by <b>Franco-Nevada Corporation</b>", # extractor markup
    "a 2.0% net smelter return royalty held by Franco-Nevada&nbsp;Corporation",   # HTML entity
])
def test_presentation_differences_are_folded_away(quote):
    """These differ from the document only in appearance. Failing them would produce false negatives,
    which is why the old rule truncated to 80 characters in the first place — it was hedging against
    exactly this. Normalising properly removes the need for the hedge."""
    assert quote_is_verbatim(quote, DOC) is True


def test_curly_quotes_fold_to_straight():
    doc = "the vendor’s retained royalty is “non-assignable”"
    assert quote_is_verbatim("the vendor's retained royalty is \"non-assignable\"", doc) is True


# ---------------------------------------------------------------- edges
@pytest.mark.parametrize("quote", ["", None, "   ", "<b></b>"])
def test_an_empty_quote_is_never_verified(quote):
    assert quote_is_verbatim(quote, DOC) is False


def test_missing_source_text_is_never_verified():
    assert quote_is_verbatim("anything at all", None) is False
    assert quote_is_verbatim("anything at all", "") is False


def test_quote_in_normalized_matches_the_one_shot_form():
    """The loop form must agree with the direct form — writers normalise the document once and then
    check every royalty against it."""
    q = "a 2.0% net smelter return royalty held by Franco-Nevada Corporation"
    assert quote_in_normalized(q, normalize(DOC)) == quote_is_verbatim(q, DOC) is True


# ---------------------------------------------------------------- the diagnostic
def test_prefix_ratio_separates_near_misses_from_fabrication():
    """Used to tell a genuine quote defeated by one character from one the model stitched together."""
    assert verbatim_prefix_ratio("a 2.0% net smelter return royalty", DOC) == 1.0
    stitched = "A separate 1.0% gross overriding royalty applies to a completely invented property."
    assert 0.0 < verbatim_prefix_ratio(stitched, DOC) < 1.0
    assert verbatim_prefix_ratio("nothing here matches at all zzz", DOC) < 0.2
